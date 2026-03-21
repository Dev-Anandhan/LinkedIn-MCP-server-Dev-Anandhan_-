"""Attempt to Easy Apply for a job on LinkedIn."""

from linkedin_mcp_server.ports.auth import AuthPort
from linkedin_mcp_server.ports.browser import BrowserPort


class ApplyJobUseCase:
    """Attempt to Easy Apply for a job on LinkedIn."""

    def __init__(self, browser: BrowserPort, auth: AuthPort, *, debug: bool = False):
        self._browser = browser
        self._auth = auth
        self._debug = debug

    async def execute(self, job_id: str) -> dict[str, str]:
        """Execute the apply job use case."""
        await self._auth.ensure_authenticated()
        success = await self._browser.apply_for_job(job_id)
        if success:
             return {"status": "success", "message": f"Successfully applied for job {job_id}."}
        else:
             return {"status": "failed", "message": f"Could not easily apply for job {job_id}. It may require manual input or not support Easy Apply."}
