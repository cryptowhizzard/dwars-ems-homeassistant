from __future__ import annotations

from typing import Any

from aiohttp import ClientSession, ClientTimeout

from .const import VALID_MODES


class SMA10SEApiClient:
    def __init__(self, session: ClientSession, api_url: str, api_token: str | None = None) -> None:
        self.session = session
        self.api_url = api_url.rstrip("/")
        self.api_token = (api_token or "").strip()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        return headers

    async def status(self) -> dict[str, Any]:
        async with self.session.get(
            f"{self.api_url}/status",
            headers=self._headers(),
            timeout=ClientTimeout(total=10),
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def set_mode(self, mode: str) -> dict[str, Any]:
        if mode not in VALID_MODES:
            raise ValueError(f"Invalid mode {mode!r}")
        async with self.session.post(
            f"{self.api_url}/set_mode",
            headers=self._headers(),
            json={"mode": mode},
            timeout=ClientTimeout(total=15),
        ) as resp:
            resp.raise_for_status()
            return await resp.json()
