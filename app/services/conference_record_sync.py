import uuid
from datetime import datetime, timezone
from typing import Any

from app.integrations.google_meet import GoogleMeetClient
from app.models.meeting import Meeting
from app.repositories.meeting_repository import MeetingRepository


def parse_iso_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        val = value.strip()
        if val.endswith("Z"):
            val = val[:-1] + "+00:00"
        parsed = datetime.fromisoformat(val)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


class ConferenceRecordSyncService:
    def __init__(
        self,
        meeting_repository: MeetingRepository,
        google_meet_client: GoogleMeetClient,
    ) -> None:
        self.meeting_repository = meeting_repository
        self.google_meet_client = google_meet_client

    def sync_conference_record(
        self,
        conference_record_name: str,
        user_id: uuid.UUID,
    ) -> Meeting:
        raw_record = self.google_meet_client.get_conference_record(
            conference_record_name
        )

        record_name = raw_record.get("name") if isinstance(raw_record.get("name"), str) else conference_record_name
        space_name = raw_record.get("space") or raw_record.get("meetingSpace")
        start_time = parse_iso_datetime(raw_record.get("startTime"))
        end_time = parse_iso_datetime(raw_record.get("endTime"))

        sync_values: dict[str, Any] = {
            "user_id": user_id,
            "conference_record_name": record_name,
        }

        if space_name is not None and isinstance(space_name, str):
            sync_values["meeting_space_name"] = space_name

        if start_time is not None:
            sync_values["start_time"] = start_time

        if end_time is not None:
            sync_values["end_time"] = end_time

        if "meetingUrl" in raw_record and isinstance(raw_record["meetingUrl"], str):
            sync_values["meeting_url"] = raw_record["meetingUrl"]

        if "title" in raw_record and isinstance(raw_record["title"], str):
            sync_values["title"] = raw_record["title"]

        return self.meeting_repository.upsert(sync_values)