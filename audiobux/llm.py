"""A local language model behind llama.cpp's server.

The server starts on the first request and stops on close(), so the card is
free again for the speech model.
"""

import base64
import logging
import shutil
import socket
import subprocess
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger(__name__)


class Llm:
    def __init__(
        self,
        model: str,
        models: Path,
        parallel: int = 8,
        context: int = 4096,
        vision: str = "",
        placement: tuple[str, ...] = ("-ngl", "999"),
        revision: Callable[[str], str | None] = lambda repo: None,
    ) -> None:
        """`model` is `repo:file.gguf` on Hugging Face or a path to a gguf file; `vision` is
        the model's mmproj file, from the same repo or a path. `placement` says how the
        layers go onto the card; `--fit on` lets a model larger than the card keep part
        of its experts in memory. `revision` names the commit of a repo to take, None for
        the latest."""
        self.model, self.models, self.revision = model, Path(models), revision
        self.parallel, self.context = parallel, context
        self.vision, self.placement = vision, placement
        self.server: subprocess.Popen[bytes] | None = None
        self.url = ""

    def ask(self, system: str, user: str, json_reply: bool = False, image: bytes | None = None) -> str:
        self._start()
        content: str | list[dict[str, Any]] = user
        if image is not None:
            url = "data:image/png;base64," + base64.b64encode(image).decode()
            content = [{"type": "image_url", "image_url": {"url": url}}, {"type": "text", "text": user}]
        body: dict[str, Any] = {
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
            "temperature": 0,
            "max_tokens": self.context // 2,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if json_reply:
            body["response_format"] = {"type": "json_object"}
        r = requests.post(self.url + "/v1/chat/completions", json=body, timeout=600)
        r.raise_for_status()
        reply: str = r.json()["choices"][0]["message"]["content"]
        return reply.strip()

    def map[T, R](self, fn: Callable[[T], R], items: list[T]) -> list[R]:
        """fn over items, as many at once as the server has slots."""
        if not items:
            return []
        self._start()
        with ThreadPoolExecutor(self.parallel) as pool:
            return list(pool.map(fn, items))

    def close(self) -> None:
        if self.server:
            self.server.terminate()
            try:
                self.server.wait(30)
            except subprocess.TimeoutExpired:
                self.server.kill()
            self.server = None

    def _start(self) -> None:
        if self.server and self.server.poll() is None:
            return
        port = _free_port()
        cmd = [
            shutil.which("llama-server") or "llama-server",
            "-m",
            str(self._file(self.model)),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            *self.placement,
            "--jinja",
            "-np",
            str(self.parallel),
            "-c",
            str(self.context * self.parallel),
            "--log-disable",
        ]
        if self.vision:
            cmd += ["--mmproj", str(self._file(self.vision))]
        log.info("starting %s", " ".join(cmd))
        server = self.server = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            if server.poll() is not None:
                raise RuntimeError(f"llama-server exited with {server.returncode}")
            try:
                if requests.get(self.url + "/health", timeout=2).ok:
                    return
            except requests.ConnectionError:
                pass
            time.sleep(1)
        self.close()
        raise RuntimeError("llama-server did not come up")

    def _file(self, spec: str) -> Path:
        if Path(spec).is_file():
            return Path(spec)
        from huggingface_hub import hf_hub_download

        repo, name = hub_file(spec, self.model)
        return Path(hf_hub_download(repo, name, revision=self.revision(repo), cache_dir=self.models / "hf"))


def hub_file(spec: str, model: str) -> tuple[str, str]:
    """(repo, file) on Hugging Face for a `repo:file.gguf` spec; a bare file name
    comes from the repo of `model`."""
    repo, _, name = spec.partition(":")
    if not name:
        repo, name = model.partition(":")[0], spec
    return repo, name


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port
