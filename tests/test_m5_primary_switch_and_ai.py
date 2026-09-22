import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.main import app
from app.models import LiveSession, LiveSessionStatus, Meeting, User
from app.repositories import MeetingDOMSegmentRepository, MeetingRepository
from app.services.meeting_ai import MeetingAIService


def create_user(db: Session, email: str) -> User:
    user = User(
        google_user_id=f"gid-{email}",
        email=email,
        display_name=email.split("@")[0].capitalize(),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def create_session(db: Session, meeting_id: uuid.UUID, user_id: uuid.UUID) -> LiveSession:
    sess = LiveSession(
        session_id=uuid.uuid4(),
        meeting_id=meeting_id,
        user_id=user_id,
        tab_session_uuid=uuid.uuid4(),
        status=LiveSessionStatus.COMPLETED.value,
    )
    db.add(sess)
    db.commit()
    db.refresh(sess)
    return sess


def test_m5_primary_transcript_view_and_ai_process_pipeline(db_session: Session) -> None:
    """Verify primary read path switch and canonical AI pipeline with Gemini 3.6 Flash."""
    user = create_user(db_session, "ai_tester@example.com")
    meeting_repo = MeetingRepository(db_session)
    dom_repo = MeetingDOMSegmentRepository(db_session)

    # 1. Create meeting
    meeting = meeting_repo.create({
        "user_id": user.id,
        "title": "M5 AI Pipeline Test",
        "status": "completed",
    })

    # 2. Add raw DOM segments
    sess = create_session(db_session, meeting.id, user.id)
    dom_repo.bulk_insert_segments(meeting.id, sess.session_id, [
        {
            "sequence": 1,
            "speaker_name": "Product Lead",
            "text": "Chúng ta quyết định release tính năng Shared Room vào ngày 20.",
            "observed_start_epoch_ms": 1700000000000,
            "observed_end_epoch_ms": 1700000004000,
        }
    ])

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_user] = lambda: user
    client = TestClient(app)

    # 3. Test GET /transcript-view (Primary read path)
    res_tv = client.get(f"/api/v1/meetings/{meeting.id}/transcript-view")
    assert res_tv.status_code == 200
    tv_data = res_tv.json()
    assert tv_data["source"] == "DOM"
    assert tv_data["total_entries"] == 1
    assert "Shared Room" in tv_data["entries"][0]["text"]

    # 4. Mock n8n client to verify AI payload and Gemini 3.6 Flash contract
    mock_n8n = MagicMock()
    mock_n8n.trigger_workflow.return_value = {
        "status": "success",
        "ai_status": "COMPLETED",
        "sheets_sync_status": "SYNCED",
        "result": {
            "summary": {
                "meeting_title": "M5 AI Pipeline Test",
                "summary": "Quyết định release Shared Room vào ngày 20.",
            },
            "decisions": [
                {
                    "id": "DEC-1",
                    "title": "Release Shared Room",
                    "context": "Release ngày 20",
                }
            ],
            "action_items": [],
            "follow_up_email": {
                "subject": "Tóm tắt cuộc họp",
                "body": "Nội dung tóm tắt",
            },
        },
    }

    ai_service = MeetingAIService(session=db_session, n8n_client=mock_n8n)
    ai_resp = ai_service.process_meeting_ai(
        meeting_id=meeting.id,
        force_reprocess=True,
    )

    assert ai_resp.status == "success"
    assert ai_resp.ai_status == "COMPLETED"
    assert ai_resp.sheets_sync_status == "SYNCED"
    assert ai_resp.ai_output is not None
    assert ai_resp.observability["model"] == "gemini-3.6-flash"

    # Verify mock n8n was called with correct payload containing the derived transcript
    mock_n8n.trigger_workflow.assert_called_once()
    called_payload = mock_n8n.trigger_workflow.call_args[0][0]
    assert called_payload["action"] == "AI_PROCESS"
    assert "Shared Room" in called_payload["transcript_prompt_text"]

    # 5. Verify Cache Hit on subsequent request
    ai_resp_cached = ai_service.process_meeting_ai(
        meeting_id=meeting.id,
        force_reprocess=False,
    )
    assert ai_resp_cached.ai_status == "COMPLETED"
    # n8n mock must NOT be called a second time
    assert mock_n8n.trigger_workflow.call_count == 1

    app.dependency_overrides.clear()
