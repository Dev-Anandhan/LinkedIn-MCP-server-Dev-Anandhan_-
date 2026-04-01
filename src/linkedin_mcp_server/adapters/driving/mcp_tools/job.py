"""Job-related MCP tool registrations."""

from typing import Any

from fastmcp import Context, FastMCP

from linkedin_mcp_server.adapters.driving.error_mapping import map_domain_error
from linkedin_mcp_server.adapters.driving.serialization import (
    serialize_scrape_response,
    serialize_sections,
)
from linkedin_mcp_server.application.apply_job import ApplyJobUseCase
from linkedin_mcp_server.application.scrape_job import ScrapeJobUseCase
from linkedin_mcp_server.application.search_jobs import SearchJobsUseCase


def register_job_tools(
    mcp: FastMCP,
    scrape_job_uc: ScrapeJobUseCase,
    search_jobs_uc: SearchJobsUseCase,
    apply_job_uc: ApplyJobUseCase,
) -> None:
    """Register job-related MCP tools."""

    @mcp.tool(
        name="get_job_details",
        description=(
            "Get job details for a specific job posting on LinkedIn.\n\n"
            "Args:\n"
            "    job_id: LinkedIn job ID (e.g., '3912045678', '4108763210')"
        ),
    )
    async def get_job_details(
        job_id: str,
        ctx: Context,
    ) -> dict[str, Any]:
        try:
            result = await scrape_job_uc.execute(job_id)
            return serialize_scrape_response(result)
        except Exception as e:
            map_domain_error(e, "get_job_details")

    @mcp.tool(
        name="search_jobs",
        description=(
            "Search for jobs on LinkedIn.\n\n"
            "Returns job_ids that can be passed to get_job_details for full info.\n\n"
            "Args:\n"
            "    keywords: Search keywords (e.g., 'backend developer', 'devops engineer')\n"
            "    location: Optional location filter (e.g., 'Austin', 'Singapore')\n"
            "    max_pages: Maximum number of result pages to load (1-10, default 3)\n"
            "    date_posted: Filter by posting date "
            "(past_hour, past_24_hours, past_week, past_month)\n"
            "    job_type: Filter by job type, comma-separated "
            "(full_time, part_time, contract, temporary, volunteer, internship, other)\n"
            "    experience_level: Filter by experience level, comma-separated "
            "(internship, entry, associate, mid_senior, director, executive)\n"
            "    work_type: Filter by work type, comma-separated (on_site, remote, hybrid)\n"
            "    easy_apply: Only show Easy Apply jobs (default false)\n"
            "    sort_by: Sort results (date, relevance)"
        ),
    )
    async def search_jobs(
        keywords: str,
        ctx: Context,
        location: str | None = None,
        max_pages: int = 3,
        date_posted: str | None = None,
        job_type: str | None = None,
        experience_level: str | None = None,
        work_type: str | None = None,
        easy_apply: bool = False,
        sort_by: str | None = None,
    ) -> dict[str, Any]:
        try:
            result = await search_jobs_uc.execute(
                keywords=keywords,
                location=location,
                max_pages=max_pages,
                date_posted=date_posted,
                job_type=job_type,
                experience_level=experience_level,
                work_type=work_type,
                easy_apply=easy_apply,
                sort_by=sort_by,
            )
            return {
                "url": result.url,
                "sections": serialize_sections(result.sections),
                "job_ids": result.job_ids,
            }
        except Exception as e:
            map_domain_error(e, "search_jobs")

    @mcp.tool(
        name="apply_for_job",
        description=(
            "Attempt to Easy Apply for a job on LinkedIn.\n\n"
            "This will navigate to the job page, click Easy Apply if available, "
            "and try to submit the application by clicking 'Next'/ 'Review' and 'Submit application'. "
            "If custom required fields are present, it will decline and return a message.\n\n"
            "Args:\n"
            "    job_id: LinkedIn job ID"
        ),
    )
    async def apply_for_job(
        job_id: str,
        ctx: Context,
    ) -> dict[str, Any]:
        try:
            return await apply_job_uc.execute(job_id)
        except Exception as e:
            map_domain_error(e, "apply_for_job")
