import uuid
from typing import Any

from app.integrations.google_meet import (
    GoogleMeetClient,
    GoogleMeetMalformedResponseError,
)
from app.models.participant import Participant
from app.repositories.meeting_repository import MeetingRepository
from app.repositories.participant_repository import ParticipantRepository
from app.services.conference_record_sync import parse_iso_datetime


class MeetingNotFoundError(ValueError):
    pass


class ParticipantSyncService:
    def __init__(
        self,
        participant_repository: ParticipantRepository,
        google_meet_client: GoogleMeetClient,
        meeting_repository: MeetingRepository | None = None,
    ) -> None:
        self.participant_repository = participant_repository
        self.google_meet_client = google_meet_client
        self.meeting_repository = meeting_repository

    def sync_participants(
        self,
        conference_record_name: str,
        meeting_id: uuid.UUID,
    ) -> list[Participant]:
        if self.meeting_repository is not None:
            meeting = self.meeting_repository.get_by_id(meeting_id)
            if meeting is None:
                raise MeetingNotFoundError(f"Meeting with id {meeting_id} not found.")

        all_items: list[dict[str, Any]] = []
        page_token: str | None = None

        while True:
            response = self.google_meet_client.list_participants(
                conference_record_name=conference_record_name,
                page_token=page_token,
            )
            all_items.extend(response.items)
            page_token = response.next_page_token
            if not page_token:
                break

        synced_participants: list[Participant] = []

        for item in all_items:
            participant_name = item.get("name")
            if not isinstance(participant_name, str) or not participant_name.strip():
                raise GoogleMeetMalformedResponseError(
                    "Participant item missing required 'name' field."
                )

            participant_type, display_name, email = self._parse_user_info(item)
            join_time = parse_iso_datetime(item.get("earliestStartTime") or item.get("startTime"))
            leave_time = parse_iso_datetime(item.get("latestEndTime") or item.get("endTime"))

            sync_values: dict[str, Any] = {
                "meeting_id": meeting_id,
                "google_participant_name": participant_name,
                "participant_type": participant_type,
            }

            if display_name is not None:
                sync_values["display_name"] = display_name
            if email is not None:
                sync_values["email"] = email
            if join_time is not None:
                sync_values["join_time"] = join_time
            if leave_time is not None:
                sync_values["leave_time"] = leave_time

            participant = self.participant_repository.upsert(sync_values)
            synced_participants.append(participant)

        return synced_participants

    @staticmethod
    def _parse_user_info(item: dict[str, Any]) -> tuple[str, str | None, str | None]:
        signed_in = item.get("signedinUser")
        anonymous = item.get("anonymousUser")
        phone = item.get("phoneUser")

        if isinstance(signed_in, dict):
            display_name = signed_in.get("displayName")
            raw_user = signed_in.get("email") or signed_in.get("user")
            email = raw_user if isinstance(raw_user, str) and "@" in raw_user else None
            if not isinstance(display_name, str):
                display_name = None
            return "signed_in_user", display_name, email

        if isinstance(anonymous, dict):
            display_name = anonymous.get("displayName")
            if not isinstance(display_name, str):
                display_name = None
            return "anonymous_user", display_name, None

        if isinstance(phone, dict):
            display_name = phone.get("displayName")
            if not isinstance(display_name, str):
                display_name = None
            return "phone_user", display_name, None

        raw_display = item.get("displayName")
        raw_email = item.get("email")
        display_name = raw_display if isinstance(raw_display, str) else None
        email = raw_email if isinstance(raw_email, str) and "@" in raw_email else None
        return "unknown", display_name, email