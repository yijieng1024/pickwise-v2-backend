"""
The admin "Price guides" page: start an extraction, read the match preview, apply.

Extraction runs in GitHub Actions, because the PDFs are up to 258 MB and Render has
512 MB of RAM. The admin pastes a Drive link and this starts the workflow; the rows
land in the staging tables, and nothing reaches a laptop until `/apply`.
"""
import re
import uuid
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


class UseGuidePriceRequest(BaseModel):
    laptop_ids: List[uuid.UUID] = Field(min_length=1)


def _drive_file_id(link: str) -> str:
    found = _DRIVE_ID.search(link)
    if not found:
        raise HTTPException(status_code=400, detail="That is not a Google Drive file link.")
    return found.group(1)


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
        headers={
            "Authorization": f"Bearer {settings.github_dispatch_token}",
            "Accept": "application/vnd.github+json",
        },
        json={
            "ref": "main",
            "inputs": {"drive_file_id": file_id, "kind": body.kind.value, "uploaded_by": str(admin.id)},
        },
        timeout=15,
    )
    if response.status_code != 204:
        logger.error("workflow dispatch failed: %s %s", response.status_code, response.text[:300])
        raise HTTPException(status_code=502, detail=f"GitHub refused the workflow start ({response.status_code}).")
    return ExtractStarted(drive_file_id=file_id, kind=body.kind, message="Extraction started.")


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
