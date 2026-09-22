"""Phase 6A — E2E Test Harness & Fixtures

Provides deterministic test fixtures, builders, and helpers for end-to-end
and integration test scenarios (Scenarios A through L).

Boundaries:
- UNIT: Pure function / schema / token optimization logic.
- INTEGRATION: Backend + DB / Backend + n8n mock / Backend + Google mock.
- E2E / SYSTEM: Simulated end-to-end pipeline (Meeting -> DOM / Official -> AI -> Sheets -> DB).
- REAL GOOGLE MEET: Browser & real manual smoke test (not synthetic).
"""

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

from sqlalchemy.orm import Session

from app.models import (
    LiveSession,
    Meeting,
    Participant,
    Transcript,
    TranscriptEntry,
    User,
)


@dataclass
class E2ETestContext:
    user: User
    meeting: Meeting
    live_session: Optional[LiveSession] = None
    official_transcript: Optional[Transcript] = None
    dom_payload: Optional[Dict[str, Any]] = None


class E2EFixtureFactory:
    """Factory for generating isolated, deterministic test models and payloads."""

    @staticmethod
    def create_test_user(
        db: Session,
        google_user_id: Optional[str] = None,
        email: Optional[str] = None,
        display_name: str = "E2E Test User",
    ) -> User:
        unique_id = uuid.uuid4().hex[:8]
        user = User(
            google_user_id=google_user_id or f"google-user-e2e-{unique_id}",
            email=email or f"e2e.user.{unique_id}@devmeet.example.com",
            display_name=display_name,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user

    @staticmethod
    def create_test_meeting(
        db: Session,
        user_id: uuid.UUID,
        title: str = "Sprint 42 Architecture Review",
        meeting_url: str = "https://meet.google.com/abc-defg-hij",
        status: str = "completed",
        ai_status: str = "NOT_PROCESSED",
        sheets_sync_status: str = "NOT_SYNCED",
        dom_capture_status: str = "not_captured",
        dom_transcript_data: Optional[Dict[str, Any]] = None,
        conference_record_name: Optional[str] = None,
    ) -> Meeting:
        unique_id = uuid.uuid4().hex[:8]
        meeting = Meeting(
            user_id=user_id,
            conference_record_name=conference_record_name or f"conferenceRecords/conf-e2e-{unique_id}",
            meeting_space_name=f"spaces/space-e2e-{unique_id}",
            meeting_url=meeting_url,
            title=title,
            start_time=datetime(2026, 9, 10, 9, 0, 0, tzinfo=timezone.utc),
            end_time=datetime(2026, 9, 10, 10, 0, 0, tzinfo=timezone.utc),
            status=status,
            ai_status=ai_status,
            sheets_sync_status=sheets_sync_status,
            dom_capture_status=dom_capture_status,
            dom_transcript_data=dom_transcript_data,
        )
        db.add(meeting)
        db.commit()
        db.refresh(meeting)
        return meeting

    @staticmethod
    def create_test_live_session(
        db: Session,
        meeting_id: uuid.UUID,
        user_id: uuid.UUID,
        tab_session_uuid: Optional[uuid.UUID] = None,
        status: str = "ACTIVE",
    ) -> LiveSession:
        session = LiveSession(
            meeting_id=meeting_id,
            user_id=user_id,
            tab_session_uuid=tab_session_uuid or uuid.uuid4(),
            status=status,
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        return session

    @staticmethod
    def add_official_transcript(
        db: Session,
        meeting: Meeting,
        entries_data: Optional[List[Dict[str, Any]]] = None,
    ) -> Transcript:
        unique_id = uuid.uuid4().hex[:8]
        transcript = Transcript(
            meeting_id=meeting.id,
            google_transcript_name=f"{meeting.conference_record_name}/transcripts/transcript-e2e-{unique_id}",
            state="FILE_GENERATED",
            docs_url=f"https://docs.google.com/document/d/{unique_id}",
        )
        db.add(transcript)
        db.commit()
        db.refresh(transcript)

        # Create participants
        p1 = Participant(
            meeting_id=meeting.id,
            google_participant_name="users/user-1",
            display_name="Nguyen Van A",
            email="nguyenvana@example.com",
        )
        p2 = Participant(
            meeting_id=meeting.id,
            google_participant_name="users/user-2",
            display_name="Tran Thi B",
            email="tranthib@example.com",
        )
        db.add_all([p1, p2])
        db.commit()
        db.refresh(p1)
        db.refresh(p2)

        default_entries = entries_data or [
            {
                "participant_id": p1.id,
                "text": "Chào mọi người, chúng ta bắt đầu cuộc họp review kiến trúc.",
                "start_time": datetime(2026, 9, 10, 9, 0, 5, tzinfo=timezone.utc),
                "end_time": datetime(2026, 9, 10, 9, 0, 15, tzinfo=timezone.utc),
            },
            {
                "participant_id": p2.id,
                "text": "Tôi đề xuất triển khai Phase 6A test harness trước khi chạy E2E full path.",
                "start_time": datetime(2026, 9, 10, 9, 0, 20, tzinfo=timezone.utc),
                "end_time": datetime(2026, 9, 10, 9, 0, 35, tzinfo=timezone.utc),
            },
            {
                "participant_id": p1.id,
                "text": "Đồng ý, chúng ta sẽ chốt quyết định này và giao cho nhóm backend.",
                "start_time": datetime(2026, 9, 10, 9, 0, 40, tzinfo=timezone.utc),
                "end_time": datetime(2026, 9, 10, 9, 0, 55, tzinfo=timezone.utc),
            },
        ]

        for idx, entry_kwargs in enumerate(default_entries):
            entry = TranscriptEntry(
                transcript_id=transcript.id,
                google_entry_name=f"{transcript.google_transcript_name}/entries/entry-{idx + 1}",
                participant_id=entry_kwargs.get("participant_id", p1.id),
                text=entry_kwargs["text"],
                start_time=entry_kwargs["start_time"],
                end_time=entry_kwargs["end_time"],
            )
            db.add(entry)

        db.commit()
        return transcript

    @staticmethod
    def create_dom_transcript_payload(
        meeting_id: str,
        segments: Optional[List[Dict[str, Any]]] = None,
        captured_at: str = "2026-09-10T09:00:55.000Z",
    ) -> Dict[str, Any]:
        default_segments = segments or [
            {
                "segment_id": "seg-dom-1",
                "speaker_name": "Nguyen Van A",
                "text": "Chào mọi người từ DOM fallback transcript.",
                "sequence": 1,
                "source": "DOM",
            },
            {
                "segment_id": "seg-dom-2",
                "speaker_name": "Tran Thi B",
                "text": "Đồng ý thực hiện test scenario DOM fallback.",
                "sequence": 2,
                "source": "DOM",
            },
        ]

        return {
            "meeting_id": meeting_id,
            "segments": default_segments,
            "segment_count": len(default_segments),
            "captured_at": captured_at,
        }

    @staticmethod
    def mock_n8n_ai_success_response(
        request_id: str,
        meeting_id: str,
        summary: str = "Cuộc họp đã thống nhất kế hoạch Phase 6A và triển khai test harness.",
    ) -> Dict[str, Any]:
        """Produces a deterministic mock response matching n8n DEVMEET_Meeting_Processor_v3.json."""
        return {
            "status": "success",
            "request_id": request_id,
            "meeting_id": meeting_id,
            "ai_output": {
                "summary": summary,
                "key_points": [
                    "Thống nhất triển khai Phase 6A",
                    "Chuẩn hóa E2E test harness",
                ],
                "decisions": [
                    {
                        "decision": "Chốt kế hoạch Phase 6A E2E harness",
                        "context": "Review kiến trúc Sprint 42",
                        "evidence_timestamp": "09:00:40",
                    }
                ],
                "action_items": [
                    {
                        "task": "Viết test suite cho Scenarios A-L",
                        "assignee": "Backend Team",
                        "deadline": "2026-09-11",
                        "status": "TODO",
                        "evidence_timestamp": "09:00:40",
                    }
                ],
                "follow_up_email": {
                    "subject": "[DevMeet AI] Biên bản cuộc họp Sprint 42",
                    "body": "Chào team,\n\nNội dung cuộc họp đã được tóm tắt...",
                },
            },
            "sheets_sync": {
                "status": "SUCCESS",
                "synced_at": datetime.now(timezone.utc).isoformat(),
                "rows_written": {
                    "meetings": 1,
                    "decisions": 1,
                    "action_items": 1,
                    "follow_up_emails": 1,
                },
            },
        }

    @staticmethod
    def mock_n8n_sheets_failure_response(
        request_id: str,
        meeting_id: str,
        summary: str = "Tóm tắt cuộc họp (Sheets sync bị lỗi).",
    ) -> Dict[str, Any]:
        """Produces a mock response where AI succeeded but Sheets sync failed."""
        return {
            "status": "partial_success",
            "request_id": request_id,
            "meeting_id": meeting_id,
            "ai_output": {
                "summary": summary,
                "key_points": ["Điểm 1", "Điểm 2"],
                "decisions": [],
                "action_items": [],
                "follow_up_email": {
                    "subject": "Email",
                    "body": "Body",
                },
            },
            "sheets_sync": {
                "status": "FAILED",
                "error": "Google Sheets API quota exceeded / network error",
            },
        }
