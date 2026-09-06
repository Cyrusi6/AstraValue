"""MinerU precision API v4; credentials stay out of logs and derived evidence."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import time
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = PROJECT_ROOT / "config/data_sources/announcement_parser.mineru.v1.json"


class MinerUError(RuntimeError):
    """Only stable, non-sensitive reason codes are included in public messages."""


def load_config(path: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    config = json.loads(path.read_text("utf-8"))
    if (config.get("provider") != "mineru-precision"
            or config.get("api_base") != "https://mineru.net/api/v4"
            or config.get("model_version") != "vlm"):
        raise MinerUError("mineru_precision_config_invalid")
    if not 1 <= config["max_pages"] <= 200 or not 1 <= config["max_file_bytes"] <= 200000000:
        raise MinerUError("mineru_file_limits_invalid")
    if config["poll_interval_seconds"] < 5 or config["poll_timeout_seconds"] <= 0:
        raise MinerUError("mineru_poll_limits_invalid")
    if config.get("fallback_proxy") not in (None, "http://127.0.0.1:7897"):
        raise MinerUError("mineru_proxy_invalid")
    return config


def load_token(env_file: Path | None = None) -> str:
    values: dict[str, str] = {}
    if env_file is not None:
        if not env_file.is_file():
            raise MinerUError("mineru_env_file_missing")
        # Parse only credential names; never execute or interpolate dotenv text.
        for line in env_file.read_text("utf-8-sig").splitlines():
            match = re.fullmatch(r"\s*(?:export\s+)?(MINERU_API|MINERU_API_TOKEN)\s*=\s*(.*?)\s*", line)
            if match:
                value = match[2]
                if value[:1] in ("'", '"') and value[-1:] == value[:1]:
                    value = value[1:-1]
                else:
                    value = value.split(" #", 1)[0].strip()
                values[match[1]] = value
    token = (os.environ.get("MINERU_API_TOKEN") or os.environ.get("MINERU_API")
             or values.get("MINERU_API_TOKEN") or values.get("MINERU_API") or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token or not re.fullmatch(r"[A-Za-z0-9._+/=-]+", token):
        raise MinerUError("mineru_token_missing_or_invalid")
    return token


def validate_url(url: str, hosts: list[str]) -> str:
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme == "https" and parsed.hostname in hosts
                 and parsed.port in (None, 443) and not parsed.username
                 and not parsed.password and not parsed.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise MinerUError("mineru_unapproved_url")
    return url


class MinerUClient:
    def __init__(self, token: str, config: dict[str, Any], *,
                 transport: httpx.BaseTransport | None = None,
                 progress: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.config = config
        self._token = token
        self._transport = transport
        self._clients: dict[str, httpx.Client] = {}
        self.route = "direct"
        self.progress = progress or (lambda event: None)

    def _client(self) -> httpx.Client:
        if self.route not in self._clients:
            self._clients[self.route] = httpx.Client(
                trust_env=False, verify=True, follow_redirects=False,
                timeout=self.config["request_timeout_seconds"],
                proxy=self.config["fallback_proxy"] if self.route == "clash" else None,
                transport=self._transport,
            )
        return self._clients[self.route]

    def _request(self, method: str, url: str, *, authenticated: bool = False,
                 content: bytes | None = None, payload: dict | None = None,
                 limit: int = 2097152) -> bytes:
        hosts = ["mineru.net"] if authenticated else self.config[
            "upload_hosts" if method == "PUT" else "download_hosts"]
        validate_url(url, hosts)
        if authenticated and not urlsplit(url).path.startswith("/api/v4/"):
            raise MinerUError("mineru_unapproved_api_path")
        headers = {"Authorization": f"Bearer {self._token}"} if authenticated else {}
        if content is not None:
            headers["Content-Length"] = str(len(content))
        for connection_try in range(2):
            try:
                with self._client().stream(method, url, headers=headers, json=payload,
                                           content=content) as response:
                    if response.status_code not in (200, 201):
                        raise MinerUError(f"mineru_http_{response.status_code}")
                    length = response.headers.get("content-length")
                    if length is not None and (not length.isdigit() or int(length) > limit):
                        raise MinerUError("mineru_response_size_exceeded")
                    chunks: list[bytes] = []
                    size = 0
                    started = time.monotonic()
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > limit:
                            raise MinerUError("mineru_response_size_exceeded")
                        if time.monotonic() - started > 600:
                            raise MinerUError("mineru_transfer_deadline")
                        chunks.append(chunk)
                    return b"".join(chunks)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                # A connection failure occurs before request transmission. Do not
                # repeat uncertain POST/PUT requests after read/write timeouts.
                if connection_try == 0 and self.route == "direct" and self.config.get("fallback_proxy"):
                    self.route = "clash"
                    self.progress({"event": "mineru_route_changed", "route": self.route})
                    continue
                raise MinerUError("mineru_connection_failed") from None
            except httpx.TimeoutException:
                raise MinerUError("mineru_transport_timeout_state_preserved") from None
            except httpx.TransportError:
                raise MinerUError("mineru_transport_failed_state_preserved") from None
        raise MinerUError("mineru_connection_failed")

    def _api(self, method: str, path: str, payload: dict | None = None) -> dict:
        raw = self._request(method, self.config["api_base"] + path,
                            authenticated=True, payload=payload)
        try:
            result = json.loads(raw)
            if type(result["code"]) is not int or result["code"] != 0:
                code = str(result["code"])
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", code):
                    code = "unknown"
                raise MinerUError("mineru_api_" + code)
            if not isinstance(result["data"], dict):
                raise ValueError()
            return result["data"]
        except (KeyError, TypeError, ValueError):
            raise MinerUError("mineru_invalid_api_response") from None

    def allocate(self, data_id: str) -> dict:
        result = self._api("POST", "/file-urls/batch", {
            "files": [{"name": data_id + ".pdf", "data_id": data_id,
                       "is_ocr": self.config["is_ocr"]}],
            **{key: self.config[key] for key in
               ("model_version", "enable_formula", "enable_table", "language")},
        })
        batch = result.get("batch_id", "")
        urls = result.get("file_urls")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", batch) or not isinstance(urls, list) or len(urls) != 1:
            raise MinerUError("mineru_invalid_upload_allocation")
        validate_url(urls[0], self.config["upload_hosts"])
        return {"batch_id": batch, "upload_url": urls[0]}

    def upload(self, url: str, content: bytes) -> None:
        self._request("PUT", url, content=content)

    def poll(self, batch_id: str, data_id: str) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", batch_id):
            raise MinerUError("mineru_invalid_batch_id")
        data = self._api("GET", "/extract-results/batch/" + batch_id)
        if data.get("batch_id") != batch_id or not isinstance(data.get("extract_result"), list):
            raise MinerUError("mineru_batch_identity_mismatch")
        matches = [r for r in data["extract_result"] if r.get("data_id") == data_id]
        if len(matches) != 1 or matches[0].get("file_name") != data_id + ".pdf":
            raise MinerUError("mineru_result_identity_mismatch")
        result = matches[0]
        if result.get("state") not in ("waiting-file", "pending", "running", "converting", "done", "failed"):
            raise MinerUError("mineru_unknown_task_state")
        return result

    def download(self, url: str) -> bytes:
        return self._request("GET", url, limit=self.config["max_zip_bytes"])

    def close(self) -> None:
        for client in self._clients.values():
            client.close()

    def __enter__(self) -> "MinerUClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
