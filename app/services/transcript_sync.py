import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.integrations.google_meet import (
    GoogleMeetClient,
    GoogleMeetMalformedResponseError,
)
from app.models.transcript import Transcript
from app.repositories.meeting_repository import MeetingRepository
from app.repositories.participant_repository import ParticipantRepository
from app.repositories.transcript_entry_repository import TranscriptEntryRepository
from app.repositories.transcript_repository import TranscriptRepository
from app.services.conference_record_sync import parse_iso_datetime


class MeetingNotFoundError(ValueError):
    pass


class TranscriptNotReadyError(Exception):
    def __init__(
        self,
        message: str,
        transcript_name: str | None = None,
        state: str | None = None,
    ) -> None:
        self.message = message
        self.transcript_name = transcript_name
        self.state = state
        self.status_code = 400
        self.error_code = "TRANSCRIPT_NOT_READY"
        super().__init__(message)


def calculate_transcript_retry_backoff(attempt: int) -> float:
    if attempt <= 1:
        return 10.0
    elif attempt == 2:
        return 30.0
    elif attempt == 3:
        return 60.0
    else:
        return min(300.0, 60.0 * (2 ** (attempt - 3)))


class TranscriptSyncService:
    def __init__(
        self,
        transcript_repository: TranscriptRepository,
        transcript_entry_repository: TranscriptEntryRepository,
        participant_repository: ParticipantRepository,
        google_meet_client: GoogleMeetClient,
        meeting_repository: MeetingRepository | None = None,
        session: Session | None = None,
    ) -> None:
        self.transcript_repository = transcript_repository
        self.transcript_entry_repository = transcript_entry_repository
        self.participant_repository = participant_repository
        self.google_meet_client = google_meet_client
        self.meeting_repository = meeting_repository
        self.session = session or getattr(transcript_repository, "session", None)

    def sync_transcripts(
        self,
        conference_record_name: str,
        meeting_id: uuid.UUID,
    ) -> list[Transcript]:
        if self.meeting_repository is not None:
            meeting = self.meeting_repository.get_by_id(meeting_id)
            if meeting is None:
                raise MeetingNotFoundError(f"Meeting with id {meeting_id} not found.")

        # 1. Fetch transcript list with pagination
        raw_transcripts: list[dict[str, Any]] = []
        page_token: str | None = None

        while True:
            response = self.google_meet_client.list_transcripts(
                conference_record_name=conference_record_name,
                page_token=page_token,
            )
            raw_transcripts.extend(response.items)
            page_token = response.next_page_token
            if not page_token:
                break

        if not raw_transcripts:
            return []

        synced_transcripts: list[Transcript] = []

        # 2. Build participant lookup map for Speaker Mapping (Part E)
        participants = self.participant_repository.list_with_pagination(page=1, page_size=1000)
        participant_map = {
            p.google_participant_name: p.id
            for p in participants
            if p.meeting_id == meeting_id
        }

        for raw_tx in raw_transcripts:
            tx_name = raw_tx.get("name")
            if not isinstance(tx_name, str) or not tx_name.strip():
                raise GoogleMeetMalformedResponseError(
                    "Transcript item missing required 'name' field."
                )

            state = raw_tx.get("state")
            if not isinstance(state, str) or not state.strip():
                raise GoogleMeetMalformedResponseError(
                    "Transcript item missing required 'state' field."
                )

            docs_url = self._extract_docs_url(raw_tx)
            now_utc = datetime.now(timezone.utc)

            # Upsert base transcript record
            tx_values: dict[str, Any] = {
                "meeting_id": meeting_id,
                "google_transcript_name": tx_name,
                "state": state,
                "processing_status": "processing",
                "fetched_at": now_utc,
            }
            if docs_url is not None:
                tx_values["docs_url"] = docs_url

            transcript = self.transcript_repository.upsert(tx_values)

            # CASE 1: state != "FILE_GENERATED"
            if state != "FILE_GENERATED":
                synced_transcripts.append(transcript)
                raise TranscriptNotReadyError(
                    message=f"Transcript '{tx_name}' is in state '{state}' and not ready for processing.",
                    transcript_name=tx_name,
                    state=state,
                )

            # CASE 2: state == "FILE_GENERATED"
            try:
                # Fetch transcript entries with pagination
                raw_entries: list[dict[str, Any]] = []
                entry_page_token: str | None = None

                while True:
                    entry_resp = self.google_meet_client.list_transcript_entries(
                        transcript_name=tx_name,
                        page_token=entry_page_token,
                    )
                    raw_entries.extend(entry_resp.items)
                    entry_page_token = entry_resp.next_page_token
                    if not entry_page_token:
                        break

                for raw_entry in raw_entries:
                    entry_name = raw_entry.get("name")
                    if not isinstance(entry_name, str) or not entry_name.strip():
                        raise GoogleMeetMalformedResponseError(
                            "Transcript entry item missing required 'name' field."
                        )

                    text = raw_entry.get("text")
                    if not isinstance(text, str):
                        raise GoogleMeetMalformedResponseError(
                            "Transcript entry item missing required 'text' field."
                        )

                    # Speaker Mapping (Part E)
                    speaker_res = raw_entry.get("participant")
                    participant_id = (
                        participant_map.get(speaker_res)
                        if isinstance(speaker_res, str)
                        else None
                    )

                    start_time = parse_iso_datetime(raw_entry.get("startTime"))
                    end_time = parse_iso_datetime(raw_entry.get("endTime"))
                    lang_code = raw_entry.get("languageCode")

                    entry_values: dict[str, Any] = {
                        "transcript_id": transcript.id,
                        "participant_id": participant_id,
                        "google_entry_name": entry_name,
                        "text": text,
                        "source": "google_meet",
                    }
                    if isinstance(lang_code, str):
                        entry_values["language_code"] = lang_code
                    if start_time is not None:
                        entry_values["start_time"] = start_time
                    if end_time is not None:
                        entry_values["end_time"] = end_time

                    self.transcript_entry_repository.upsert(entry_values)

                # Update processing status to completed upon successful sync
                updated_tx = self.transcript_repository.update(
                    transcript.id,
                    {"processing_status": "completed"},
                )
                synced_transcripts.append(updated_tx or transcript)

            except Exception:
                # Set processing_status = "failed" and rollback session if present
                self.transcript_repository.update(
                    transcript.id,
                    {"processing_status": "failed"},
                )
                if self.session is not None:
                    self.session.rollback()
                raise

        return synced_transcripts

    @staticmethod
    def _extract_docs_url(raw_tx: dict[str, Any]) -> str | None:
        docs_dest = raw_tx.get("docsDestination")
        if isinstance(docs_dest, dict):
            doc = docs_dest.get("document")
            if isinstance(doc, str):
                return doc
        docs_url = raw_tx.get("docsUrl") or raw_tx.get("docs_url")
        if isinstance(docs_url, str):
            return docs_url
        return None