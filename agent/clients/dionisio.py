import httpx
from typing import Any, Optional
from config import DIONISIO_API_KEY, DIONISIO_BASE_URL


class DionisioAPIError(Exception):
    def __init__(self, status_code: int, message: str, code: str = ""):
        self.status_code = status_code
        self.message = message
        self.code = code
        super().__init__(f"[{status_code}] {message}")


class DionisioClient:
    """
    HTTP client for the Dionísio CRM API.
    Uses a persistent httpx.Client for connection pooling across tool calls.
    """

    def __init__(self):
        self._client = httpx.Client(
            base_url=DIONISIO_BASE_URL,
            headers={
                "Authorization": f"Bearer {DIONISIO_API_KEY}",
                "Content-Type": "application/json",
            },
            timeout=15.0,
        )

    def __del__(self):
        try:
            self._client.close()
        except Exception:
            pass

    def call(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        body: Optional[dict] = None,
    ) -> Any:
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        clean_body = {k: v for k, v in (body or {}).items() if v is not None}

        response = self._client.request(
            method=method.upper(),
            url=path,
            params=clean_params or None,
            json=clean_body or None,
        )

        if not response.is_success:
            try:
                err = response.json()
                msg = err.get("error", response.text)
                code = err.get("code", "")
            except Exception:
                msg = response.text
                code = ""
            raise DionisioAPIError(response.status_code, msg, code)

        if response.content:
            return response.json()
        return {}

    def execute_tool(self, tool_def: "ToolDefinition", params: dict) -> Any:  # noqa: F821
        """Generic tool executor: routes params to path / query / body based on method."""
        path = tool_def.path_template
        resolved_params = dict(params)

        # Substitute path params
        for p in tool_def.path_params:
            value = resolved_params.pop(p, None)
            if value is None:
                raise ValueError(
                    f"Missing required path param '{p}' for tool '{tool_def.name}'"
                )
            path = path.replace(f"{{{p}}}", str(value))

        # GET → query string; everything else → body
        if tool_def.method.upper() == "GET":
            return self.call("GET", path, params=resolved_params)
        else:
            return self.call(tool_def.method, path, body=resolved_params)
