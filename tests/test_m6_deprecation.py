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


def test_m6_full_operation_without_legacy_dom_transcript_data(db_session: Session) -> None:
    """Verify that the system operates 100% cleanly when dom_transcript_data column is completely NULL."""
    user = create_user(db_session, "m6_user@example.com")
    meeting_repo = MeetingRepository(db_session)
    dom_repo = MeetingDOMSegmentRepository(db_session)

    # 1. Create meeting with dom_transcript_data=None (simulating deprecated / cleaned-up legacy column)
    meeting = meeting_repo.create({
        "user_id": user.id,
        "title": "M6 Deprecation Test Meeting",
        "status": "completed",
        "dom_transcript_data": None,  # Explicitly NULL
    })

    # 2. Add raw segments only into meeting_dom_segments
    sess = create_session(db_session, meeting.id, user.id)
    dom_repo.bulk_insert_segments(meeting.id, sess.session_id, [
        {
            "sequence": 1,
            "speaker_name": "Speaker 1",
            "text": "Nội dung cuộc họp được lưu trữ hoàn toàn trong bảng raw segments.",
            "observed_start_epoch_ms": 1700000000000,
            "observed_end_epoch_ms": 1700000003000,
        }
    ])

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_user] = lambda: user
    client = TestClient(app)

    # 3. Verify transcript-view works perfectly
    res_tv = client.get(f"/api/v1/meetings/{meeting.id}/transcript-view")
    assert res_tv.status_code == 200
    tv_data = res_tv.json()
    assert tv_data["source"] == "DOM"
    assert tv_data["total_entries"] == 1
    assert "raw segments" in tv_data["entries"][0]["text"]

    # 4. Verify AI process works through canonical endpoint
    mock_n8n = MagicMock()
    mock_n8n.trigger_workflow.return_value = {
        "status": "success",
        "ai_status": "COMPLETED",
        "sheets_sync_status": "SYNCED",
        "result": {
            "summary": {
                "meeting_title": "M6 Deprecation Test",
                "summary": "Tóm tắt từ raw segments",
            }
        },
    }

    ai_service = MeetingAIService(session=db_session, n8n_client=mock_n8n)
    res_ai = ai_service.process_meeting_ai(meeting.id, force_reprocess=True)
    assert res_ai.status == "success"
    assert res_ai.ai_status == "COMPLETED"

    # 5. Verify legacy endpoint aliases /ai-process and /ai-status still route correctly
    res_legacy_ai = client.post(f"/api/v1/meetings/{meeting.id}/ai-process", json={})
    assert res_legacy_ai.status_code == 200
    assert res_legacy_ai.json()["ai_status"] == "COMPLETED"

    res_legacy_status = client.get(f"/api/v1/meetings/{meeting.id}/ai-status")
    assert res_legacy_status.status_code == 200
    assert res_legacy_status.json()["ai_status"] == "COMPLETED"

    app.dependency_overrides.clear()
