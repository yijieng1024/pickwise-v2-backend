"""
The admin "Price guides" page: start an extraction, read the match preview, apply.

Extraction runs in GitHub Actions, because the PDFs are up to 258 MB and Render has
512 MB of RAM. The admin pastes a Drive link and this starts the workflow; the rows
land in the staging tables, and nothing reaches a laptop until `/apply`.
"""
import re
import uuid
from datetime import datetime
from typing import List, Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.common.job_model import JobAccepted, JobType
from app.common.job_service import create_job, job_accepted, refuse_if_running, run_job
from app.config import settings
from app.database import get_session
from app.laptops.price_guide_model import PriceGuide, PriceGuideKind
from app.laptops.price_guide_service import GuideInUse, NotSelectable, apply_guide, delete_guide, preview, use_guide_price
from app.logger import get_logger
from app.users.auth import get_current_admin
from app.users.models import User

logger = get_logger(__name__)

router = APIRouter(
    prefix="/admin/price-guides",
    tags=["Admin - Price Guides"],
    dependencies=[Depends(get_current_admin)],
)

_DRIVE_ID = re.compile(r"(?:/d/|[?&]id=)([\w-]{20,})")
_WORKFLOW = "price-guide.yml"


class ExtractRequest(BaseModel):
    drive_link: str
    kind: PriceGuideKind


class ExtractStarted(BaseModel):
    drive_file_id: str
    kind: PriceGuideKind
    message: str
    # Null when GitHub answers 204, which names no run.
    run_id: Optional[int] = None


class ExtractionRun(BaseModel):
    run_id: int
    # GitHub's values: queued, in_progress, completed and a few waiting states.
    status: str
    conclusion: Optional[str] = None
    url: str
    created_at: datetime
    # The step running now, or the one that failed.
    step: Optional[str] = None
    # Set once a successful run has written; null for a run older than these columns.
    guide_id: Optional[uuid.UUID] = None
    filename: Optional[str] = None
    note: Optional[str] = None
    is_new_guide: bool = False


class UseGuidePriceRequest(BaseModel):
    laptop_ids: List[uuid.UUID] = Field(min_length=1)


def _drive_file_id(link: str) -> str:
    found = _DRIVE_ID.search(link)
    if not found:
        raise HTTPException(status_code=400, detail="That is not a Google Drive file link.")
    return found.group(1)


def _github_headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.github_dispatch_token}",
        "Accept": "application/vnd.github+json",
    }


def _github_get(path: str) -> dict:
    url = f"https://api.github.com/repos/{settings.github_repository}/actions/{path}"
    try:
        response = httpx.get(url, headers=_github_headers(), timeout=15)
    except httpx.HTTPError as error:
        logger.error("workflow status failed: %s", type(error).__name__)
        raise HTTPException(status_code=502, detail="GitHub could not be reached.")
    if response.status_code != 200:
        logger.error("workflow status failed: %s %s", response.status_code, response.text[:300])
        raise HTTPException(status_code=502, detail=f"GitHub refused the status check ({response.status_code}).")
    return response.json()


def _current_step(run_id: int) -> Optional[str]:
    """The step running now, or the first that failed."""
    steps = [step for job in _github_get(f"runs/{run_id}/jobs").get("jobs", []) for step in job.get("steps", [])]
    return next((s["name"] for s in steps if s.get("status") == "in_progress" or s.get("conclusion") == "failure"), None)


def _guide_or_404(session: Session, guide_id: uuid.UUID) -> PriceGuide:
    guide = session.get(PriceGuide, guide_id)
    if guide is None:
        raise HTTPException(status_code=404, detail="Price guide not found")
    return guide


@router.get("", response_model=List[PriceGuide])
def list_price_guides(session: Session = Depends(get_session)):
    """Newest first."""
    return session.exec(select(PriceGuide).order_by(PriceGuide.uploaded_at.desc())).all()  # type: ignore[attr-defined]


@router.post("/extract", response_model=ExtractStarted, status_code=status.HTTP_202_ACCEPTED)
def start_extraction(body: ExtractRequest, admin: User = Depends(get_current_admin)) -> ExtractStarted:
    """
    Start the extraction workflow for a PDF in the shared Drive folder. The workflow
    takes a few minutes; the guide then appears in `GET /admin/price-guides`.
    400 without `GITHUB_DISPATCH_TOKEN`, or for a link that is not a Drive file.
    """
    file_id = _drive_file_id(body.drive_link)
    if not settings.github_dispatch_token:
        raise HTTPException(status_code=400, detail="GITHUB_DISPATCH_TOKEN is not set, so the workflow cannot be started.")

    response = httpx.post(
        f"https://api.github.com/repos/{settings.github_repository}/actions/workflows/{_WORKFLOW}/dispatches",
        headers=_github_headers(),
        json={
            "ref": "main",
            "inputs": {"drive_file_id": file_id, "kind": body.kind.value, "uploaded_by": str(admin.id)},
        },
        timeout=15,
    )
    # GitHub's docs say 200 with the run id; 204 with no body is what it answered before.
    if response.status_code not in (200, 204):
        logger.error("workflow dispatch failed: %s %s", response.status_code, response.text[:300])
        raise HTTPException(status_code=502, detail=f"GitHub refused the workflow start ({response.status_code}).")
    run_id = response.json().get("workflow_run_id") if response.status_code == 200 else None
    return ExtractStarted(drive_file_id=file_id, kind=body.kind, message="Extraction started.", run_id=run_id)


@router.get("/extraction", response_model=Optional[ExtractionRun])
def extraction_run(run_id: Optional[int] = None, session: Session = Depends(get_session)) -> Optional[ExtractionRun]:
    """
    One workflow run as GitHub reports it: `run_id`, or the latest. Null when there
    has been no run. Stateless, so a reload finds a running extraction again. Once a
    run succeeds, `note` says what it wrote. 400 without `GITHUB_DISPATCH_TOKEN`, 502
    when GitHub cannot answer. No database read happens until GitHub has answered.
    """
    if not settings.github_dispatch_token:
        raise HTTPException(status_code=400, detail="GITHUB_DISPATCH_TOKEN is not set, so the workflow cannot be read.")

    if run_id is None:
        runs = _github_get(f"workflows/{_WORKFLOW}/runs?per_page=1").get("workflow_runs", [])
        if not runs:
            return None
        run = runs[0]
    else:
        run = _github_get(f"runs/{run_id}")

    created_at = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
    found = ExtractionRun(
        run_id=run["id"], status=run["status"], conclusion=run.get("conclusion"), url=run["html_url"], created_at=created_at,
    )
    if run.get("conclusion") == "success":
        guide = session.exec(select(PriceGuide).where(PriceGuide.last_run_id == found.run_id)).first()
        if guide is not None:
            found.guide_id, found.filename, found.note = guide.id, guide.filename, guide.last_run_note
            found.is_new_guide = guide.uploaded_at >= created_at
    else:
        found.step = _current_step(found.run_id)
    return found


@router.get("/{guide_id}/preview")
def preview_price_guide(guide_id: uuid.UUID, session: Session = Depends(get_session)) -> dict:
    """What an apply would write, and everything it would leave for a human. Reads only."""
    return preview(session, _guide_or_404(session, guide_id))


@router.post("/{guide_id}/apply", response_model=JobAccepted, status_code=status.HTTP_202_ACCEPTED)
def apply_price_guide(
    guide_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    admin: Optional[User] = Depends(get_current_admin),
) -> JobAccepted:
    """
    Write the guide's `matched` prices. **Returns 202 with a job.** One transaction,
    so a failed apply writes nothing. Applying a guide twice changes nothing. 409
    while an apply is running.
    """
    guide = _guide_or_404(session, guide_id)
    refuse_if_running(session, (JobType.PRICE_GUIDE_APPLY,), "A price guide apply is already running.")
    job = create_job(
        session,
        job_type=JobType.PRICE_GUIDE_APPLY,
        total_count=guide.row_count,
        params={"guide_id": str(guide_id)},
        created_by=admin.id,
    )

    def worker(work_session: Session, progress) -> dict:
        return apply_guide(work_session, guide_id, progress)

    background_tasks.add_task(run_job, job.id, worker)
    return job_accepted(job, message=f"Applying {guide.filename} in the background.")


@router.post("/{guide_id}/use-guide-price")
def use_price_from_guide(
    guide_id: uuid.UUID,
    body: UseGuidePriceRequest,
    session: Session = Depends(get_session),
) -> dict:
    """
    "Use guide price" on the differs, suffix-rule or base-code list, for one laptop or a
    selection. Writes as an apply does, so a later guide can update these prices; a
    laptop not selected keeps its price. All or nothing: 409 if any id is no longer on
    a list, or while an apply is running.
    """
    guide = _guide_or_404(session, guide_id)
    refuse_if_running(session, (JobType.PRICE_GUIDE_APPLY,), "A price guide apply is already running.")
    try:
        return use_guide_price(session, guide, set(body.laptop_ids))
    except NotSelectable as error:
        raise HTTPException(status_code=409, detail=str(error))


@router.delete("/{guide_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_price_guide(guide_id: uuid.UUID, session: Session = Depends(get_session)) -> None:
    """
    Permanently delete a guide and its rows. The same PDF can then be extracted again.
    409 if a laptop price came from it, or while an apply is running.
    """
    guide = _guide_or_404(session, guide_id)
    refuse_if_running(session, (JobType.PRICE_GUIDE_APPLY,), "A price guide apply is running.")
    try:
        delete_guide(session, guide)
    except GuideInUse as error:
        raise HTTPException(status_code=409, detail=str(error))
