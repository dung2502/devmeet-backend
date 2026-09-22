import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.config import get_settings
from app.database import get_db
from app.integrations.google_meet import GoogleMeetClient
from app.models.user import User
from app.repositories import MeetingAccessRepository, MeetingRepository, UserRepository
from app.schemas.ai import (
    MeetingAIProcessRequest,
    MeetingAIProcessResponse,
    MeetingAIStatusResponse,
)
from app.schemas.meeting import (
    MeetingDetailResponse,
    MeetingEndedResponse,
    MeetingListItemResponse,
    MeetingListResponse,
    MeetingResponse,
    MeetingSyncRequest,
)
from app.schemas.transcript_view import TranscriptViewResponse
from app.services.conference_record_sync import ConferenceRecordSyncService
from app.services.ended_service import EndedService
from app.services.export_service import ExportService
from app.services.meeting_ai import MeetingAIService
from app.services.participant_sync import MeetingNotFoundError
from app.services.transcript_source_selector import TranscriptSourceSelector
from app.services.transcript_view_service import TranscriptViewService

router = APIRouter(prefix="/meetings", tags=["meetings"])


def resolve_meeting_participants(meeting: Any, db: Session) -> list[dict[str, Any]]:
    """
    Resolves comprehensive participant list for a meeting across all ingestion paths:
    1. Official Google Meet REST API participants (if synced).
    2. DOM Live Captions speaker names from MeetingDOMSegment or dom_transcript_data.
    3. Meeting owner/access participants as fallback if no speakers/participants recorded yet.
    """
    participants_data: list[dict[str, Any]] = []
    known_names: set[str] = set()

    # 1. Official participants from database
    for p in (meeting.participants or []):
        name = (p.display_name or "").strip()
        if name:
            known_names.add(name.lower())
        participants_data.append({
            "id": str(p.id),
            "display_name": p.display_name or name or "Thành viên",
            "email": p.email,
            "participant_type": p.participant_type,
        })

    # 2. Extract DOM segment speakers if any
    from app.models.meeting_dom_segment import MeetingDOMSegment
    dom_speakers = list(
        db.scalars(
            select(MeetingDOMSegment.speaker_name)
            .where(
                MeetingDOMSegment.meeting_id == meeting.id,
                MeetingDOMSegment.speaker_name.is_not(None),
                MeetingDOMSegment.speaker_name != "",
                MeetingDOMSegment.speaker_name != "Unknown",
                MeetingDOMSegment.speaker_name != "Unknown Speaker",
            )
            .distinct()
        ).all()
    )

    # Fallback to legacy dom_transcript_data if MeetingDOMSegment empty
    if not dom_speakers and meeting.dom_transcript_data:
        from app.services.dom_transcript_normalizer import extract_dom_entries, resolve_dom_speaker
        raw_dom_entries = extract_dom_entries(meeting.dom_transcript_data)
        dom_speakers = list({
            resolve_dom_speaker(e, default_speaker="")
            for e in raw_dom_entries
            if resolve_dom_speaker(e, default_speaker="").strip() not in ("", "Unknown", "Unknown Speaker")
        })

    for speaker in dom_speakers:
        cleaned_speaker = speaker.strip()
        if cleaned_speaker and cleaned_speaker.lower() not in known_names:
            known_names.add(cleaned_speaker.lower())
            speaker_email = None
            if meeting.user and meeting.user.display_name and meeting.user.display_name.strip().lower() == cleaned_speaker.lower():
                speaker_email = meeting.user.email

            part_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{meeting.id}_{cleaned_speaker}"))
            participants_data.append({
                "id": part_id,
                "display_name": cleaned_speaker,
                "email": speaker_email,
                "participant_type": "signed_in_user",
            })

    # 3. If still empty, add meeting owner/creator
    if not participants_data and meeting.user:
        owner_name = meeting.user.display_name or meeting.user.email or "Host"
        participants_data.append({
            "id": str(meeting.user.id),
            "display_name": owner_name,
            "email": meeting.user.email,
            "participant_type": "host",
        })

    return participants_data


def count_meeting_transcript_entries(meeting: Any, db: Session) -> int:
    """
    Counts transcript entries hierarchically across all sources:
    1. Official Google Meet REST Transcript entries.
    2. meeting_dom_segments table records.
    3. JSONB dom_transcript_data (sessions / segments).
    """
    # 1. Official Google Meet REST Transcript entries
    if getattr(meeting, "transcripts", None):
        from app.models.transcript_entry import TranscriptEntry
        from app.models.transcript import Transcript
        official_count = db.scalar(
            select(func.count(TranscriptEntry.id))
            .join(Transcript, Transcript.id == TranscriptEntry.transcript_id)
            .where(Transcript.meeting_id == meeting.id)
        )
        if official_count and official_count > 0:
            return official_count

    # 2. meeting_dom_segments table records
    from app.models.meeting_dom_segment import MeetingDOMSegment
    dom_table_count = db.scalar(
        select(func.count(MeetingDOMSegment.segment_id))
        .where(MeetingDOMSegment.meeting_id == meeting.id)
    )
    if dom_table_count and dom_table_count > 0:
        return dom_table_count

    # 3. JSONB dom_transcript_data
    if meeting.dom_transcript_data:
        from app.services.dom_transcript_normalizer import extract_dom_entries
        dom_entries = extract_dom_entries(meeting.dom_transcript_data)
        if dom_entries:
            return len(dom_entries)

    return 0


@router.get("", response_model=MeetingListResponse, status_code=status.HTTP_200_OK)
def list_meetings(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    sort: str = Query("start_time_desc"),
    search: str | None = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Any:
    """
    List all meetings belonging to the authenticated user.
    Supports pagination, keyword search, and sorting.
    Enforces strict user isolation.
    """
    meeting_repo = MeetingRepository(db)
    selector = TranscriptSourceSelector(db)

    items, total = meeting_repo.list_by_user(
        user_id=current_user.id,
        page=page,
        page_size=page_size,
        sort=sort,
        search=search,
    )

    access_repo = MeetingAccessRepository(db)
    response_items: list[MeetingListItemResponse] = []
    for m in items:
        sel_res = selector.select_source_by_meeting_id(m.id)
        resolved_parts = resolve_meeting_participants(m, db)
        part_count = len(resolved_parts)
        entry_count = count_meeting_transcript_entries(m, db)
        code = m.meeting_code
        norm_title = m.title
        if not norm_title or norm_title.strip().lower() in ("meet", "google meet", "google meet session"):
            norm_title = f"Meet - {code}" if code else "Meet"

        user_role_db = access_repo.get_user_role(m.id, current_user.id)
        calculated_role = "OWNER" if m.user_id == current_user.id else (user_role_db or "PARTICIPANT")
        host_name = m.user.display_name or m.user.email if m.user else None

        response_items.append(
            MeetingListItemResponse(
                id=m.id,
                user_id=m.user_id,
                conference_record_name=m.conference_record_name,
                meeting_space_name=m.meeting_space_name,
                meeting_url=m.meeting_url,
                meeting_code=code,
                title=norm_title,
                start_time=m.start_time,
                end_time=m.end_time,
                status=m.status,
                transcript_status=m.transcript_status,
                dom_capture_status=m.dom_capture_status,
                ai_status=m.ai_status,
                sheets_sync_status=m.sheets_sync_status,
                selected_transcript_source=sel_res.selected_source.upper(),
                participant_count=part_count,
                transcript_entry_count=entry_count,
                user_role=calculated_role,
                host_name=host_name,
                created_at=m.created_at,
                updated_at=m.updated_at,
            )
        )

    return MeetingListResponse(
        items=response_items,
        page=page,
        page_size=page_size,
        total=total,
    )


@router.post("/sync", response_model=MeetingResponse, status_code=status.HTTP_200_OK)
def sync_meeting(
    payload: MeetingSyncRequest,
    current_user: User = Depends(get_current_user),
    x_google_access_token: str | None = Header(None, alias="X-Google-Access-Token"),
    access_token: str | None = Query(None),
    db: Session = Depends(get_db),
) -> Any:
    """
    Multi-Tier Conference Identity Resolution endpoint.
    Resolves client request to a shared room meeting_id and registers user in meeting_access.
    """
    from app.services.conference_identity_resolver import ConferenceIdentityResolver

    resolver = ConferenceIdentityResolver(db)
    meeting, is_created = resolver.resolve_or_create_shared_meeting(
        user_id=current_user.id,
        conference_record_name=payload.conference_record_name,
        meeting_space_name=payload.meeting_space_name,
        meeting_url=payload.meeting_url,
        title=payload.title,
        start_time=payload.start_time,
        end_time=payload.end_time,
        meeting_id=payload.meeting_id,
    )

    token = x_google_access_token or access_token
    if payload.conference_record_name and token:
        try:
            client = GoogleMeetClient(access_token=token)
            service = ConferenceRecordSyncService(MeetingRepository(db), client)
            meeting = service.sync_conference_record(
                conference_record_name=payload.conference_record_name,
                user_id=current_user.id,
            )
        except Exception as exc:
            pass

    return meeting


@router.get("/{meeting_id}", response_model=MeetingDetailResponse, status_code=status.HTTP_200_OK)
def get_meeting(
    meeting_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Any:
    from app.repositories.meeting_access_repository import MeetingAccessRepository

    meeting_repo = MeetingRepository(db)
    access_repo = MeetingAccessRepository(db)

    meeting = meeting_repo.get_by_id(meeting_id)
    if meeting is None:
        raise MeetingNotFoundError(f"Meeting with id {meeting_id} not found.")

    user_role = access_repo.get_user_role(meeting_id, current_user.id)
    if meeting.user_id != current_user.id and user_role is None:
        raise MeetingNotFoundError(f"Meeting with id {meeting_id} not found.")


    settings = get_settings()
    selector = TranscriptSourceSelector(db)
    sel_res = selector.select_source_by_meeting_id(meeting.id)

    sheets_url = (
        f"https://docs.google.com/spreadsheets/d/{settings.devmeet_spreadsheet_id}"
        if settings.devmeet_spreadsheet_id and meeting.sheets_sync_status == "SYNCED"
        else None
    )

    participants_data = resolve_meeting_participants(meeting, db)

    code = meeting.meeting_code
    norm_title = meeting.title
    if not norm_title or norm_title.strip().lower() in ("meet", "google meet", "google meet session"):
        norm_title = f"Meet - {code}" if code else "Meet"

    calculated_role = "OWNER" if meeting.user_id == current_user.id else (user_role or "PARTICIPANT")
    host_name = meeting.user.display_name or meeting.user.email if meeting.user else None
    entry_count = count_meeting_transcript_entries(meeting, db)

    return MeetingDetailResponse(
        id=meeting.id,
        user_id=meeting.user_id,
        conference_record_name=meeting.conference_record_name,
        meeting_space_name=meeting.meeting_space_name,
        meeting_url=meeting.meeting_url,
        meeting_code=code,
        title=norm_title,
        start_time=meeting.start_time,
        end_time=meeting.end_time,
        status=meeting.status,
        transcript_status=meeting.transcript_status,
        dom_capture_status=meeting.dom_capture_status,
        ai_status=meeting.ai_status,
        sheets_sync_status=meeting.sheets_sync_status,
        google_sheets_url=sheets_url,
        ai_result=meeting.ai_result,
        comparison_status=meeting.comparison_status,
        comparison_metrics=meeting.comparison_metrics,
        selected_transcript_source=sel_res.selected_source.upper(),
        participants=participants_data,
        transcript_entry_count=entry_count,
        user_role=calculated_role,
        host_name=host_name,
        created_at=meeting.created_at,
        updated_at=meeting.updated_at,
    )


@router.get(
    "/{meeting_id}/transcript-view",
    response_model=TranscriptViewResponse,
    status_code=status.HTTP_200_OK,
    summary="Get clean, normalized transcript entries via Hybrid Selection",
    description="Deterministically selects Official > DOM > None without merging.",
)
def get_transcript_view(
    meeting_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Any:
    service = TranscriptViewService(session=db)
    return service.get_transcript_view(meeting_id=meeting_id, user_id=current_user.id)


@router.get(
    "/{meeting_id}/export",
    status_code=status.HTTP_200_OK,
    summary="Export meeting transcript or AI summary as plain text file",
)
def export_meeting(
    meeting_id: uuid.UUID,
    type: str = Query("transcript", description="Export type: transcript | summary | all"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PlainTextResponse:
    if type not in ("transcript", "summary", "all"):
        type = "transcript"

    service = ExportService(session=db)
    content, filename = service.export_meeting(
        meeting_id=meeting_id,
        export_type=type,  # type: ignore
        user_id=current_user.id,
    )
    return PlainTextResponse(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@router.post(
    "/{meeting_id}/ai/process",
    response_model=MeetingAIProcessResponse,
    status_code=status.HTTP_200_OK,
)
@router.post(
    "/{meeting_id}/ai-process",
    response_model=MeetingAIProcessResponse,
    status_code=status.HTTP_200_OK,
    include_in_schema=False,
)
def process_meeting_ai(
    meeting_id: uuid.UUID,
    payload: MeetingAIProcessRequest = MeetingAIProcessRequest(),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Any:
    from app.repositories.meeting_access_repository import MeetingAccessRepository

    meeting_repo = MeetingRepository(db)
    access_repo = MeetingAccessRepository(db)

    meeting = meeting_repo.get_by_id(meeting_id)
    if meeting is None:
        raise MeetingNotFoundError(f"Meeting with id {meeting_id} not found.")

    user_role = access_repo.get_user_role(meeting_id, current_user.id)

    # Guard: PARTICIPANT cannot force reprocess when AI result is already present
    if user_role == "PARTICIPANT" and meeting.user_id != current_user.id and payload.force_reprocess and meeting.ai_result is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Chỉ chủ phòng mới có thể yêu cầu phân tích lại AI.",
        )

    service = MeetingAIService(session=db)
    return service.process_meeting_ai(
        meeting_id=meeting_id,
        request_id=payload.request_id,
        tasks=payload.tasks,
        force_reprocess=payload.force_reprocess,
    )


@router.get(
    "/{meeting_id}/ai/status",
    response_model=MeetingAIStatusResponse,
    status_code=status.HTTP_200_OK,
)
@router.get(
    "/{meeting_id}/ai-status",
    response_model=MeetingAIStatusResponse,
    status_code=status.HTTP_200_OK,
    include_in_schema=False,
)
def get_meeting_ai_status(
    meeting_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> Any:
    service = MeetingAIService(session=db)
    return service.get_meeting_ai_status(meeting_id=meeting_id)


# ─── Phase 5D: Meeting Ended ─────────────────────────────────────────────────

@router.post(
    "/{meeting_id}/ended",
    response_model=MeetingEndedResponse,
    status_code=status.HTTP_200_OK,
    summary="Mark a meeting as ended",
    description=(
        "Signals that the meeting has ended. "
        "Updates meeting status to 'completed' and transcript_status to 'processing'. "
        "Schedules/triggers Official Transcript Sync if conference_record_name and Google token are available. "
        "Fully idempotent — safe to call multiple times."
    ),
)
def meeting_ended(
    meeting_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    authorization: str | None = Header(None, alias="Authorization"),
    x_google_access_token: str | None = Header(None, alias="X-Google-Access-Token"),
    access_token_query: str | None = Query(None, alias="access_token"),
    db: Session = Depends(get_db),
) -> Any:
    # Resolve token from headers/query
    token = None
    if authorization:
        parts = authorization.strip().split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1].strip()
        elif len(parts) == 1 and parts[0].lower() != "bearer":
            token = parts[0].strip()
    if not token:
        token = x_google_access_token or access_token_query

    service = EndedService(session=db)
    return service.mark_meeting_ended(
        meeting_id=meeting_id,
        user_id=current_user.id,
        google_access_token=token,
        background_tasks=background_tasks,
    )


# ─── Meeting Deletion ─────────────────────────────────────────────────────────

@router.delete(
    "/{meeting_id}",
    status_code=status.HTTP_200_OK,
    summary="Delete a meeting",
    description=(
        "Deletes a meeting and cascades deletion to all associated records "
        "(participants, transcripts, entries, live sessions, access records, dom segments). "
        "Only the meeting owner or a user with host role can delete the meeting."
    ),
)
def delete_meeting(
    meeting_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    from app.repositories.meeting_access_repository import MeetingAccessRepository

    meeting_repo = MeetingRepository(db)
    access_repo = MeetingAccessRepository(db)

    meeting = meeting_repo.get_by_id(meeting_id)
    if meeting is None:
        raise MeetingNotFoundError(f"Meeting with id {meeting_id} not found.")

    user_role = access_repo.get_user_role(meeting_id, current_user.id)
    is_owner = (meeting.user_id == current_user.id) or (user_role in ("OWNER", "host"))

    # Security check: User must be either owner or granted participant access
    if not is_owner and user_role is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Bạn không có quyền thao tác trên cuộc họp này.",
        )

    # Protection: Cannot delete or hide meeting while it is in_progress
    if meeting.status == "in_progress":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Không thể xóa cuộc họp đang diễn ra. Vui lòng kết thúc cuộc họp trước.",
        )

    # Reference Counting / Last-One-Out logic:
    # Find all OTHER users who still hold access to this meeting
    all_accesses = access_repo.list_by_meeting_id(meeting_id)
    remaining_accesses = [acc for acc in all_accesses if acc.user_id != current_user.id]

    # Case 1: Other users still hold access -> Soft Remove for current user & Transfer Ownership if needed
    if len(remaining_accesses) > 0:
        access_repo.remove_access(meeting_id, current_user.id)

        # If current user is owner, transfer ownership to the next remaining user
        if is_owner or meeting.user_id == current_user.id:
            next_owner_access = remaining_accesses[0]
            meeting.user_id = next_owner_access.user_id
            next_owner_access.role = "OWNER"
            db.commit()

        return {
            "success": True,
            "message": "Cuộc họp đã được gỡ khỏi danh sách của bạn.",
            "meeting_id": str(meeting_id),
            "action": "soft_remove",
            "remaining_users_count": len(remaining_accesses),
        }

    # Case 2: No other users remain (current user is the last one) -> Hard Delete
    access_repo.remove_access(meeting_id, current_user.id)
    success = meeting_repo.delete(meeting_id)
    if not success:
        raise MeetingNotFoundError(f"Meeting with id {meeting_id} not found.")

    return {
        "success": True,
        "message": "Cuộc họp đã được xóa hoàn toàn khỏi hệ thống.",
        "meeting_id": str(meeting_id),
        "action": "hard_delete",
        "remaining_users_count": 0,
    }