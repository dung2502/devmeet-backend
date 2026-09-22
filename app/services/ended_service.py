import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.integrations.google_meet import GoogleMeetClient
from app.models.meeting_access import MeetingRole
from app.repositories.live_session_repository import LiveSessionRepository
from app.repositories.meeting_access_repository import MeetingAccessRepository
from app.repositories.meeting_repository import MeetingRepository
from app.repositories.participant_repository import ParticipantRepository
from app.repositories.transcript_entry_repository import TranscriptEntryRepository
from app.repositories.transcript_repository import TranscriptRepository
from app.schemas.meeting import MeetingEndedResponse
from app.services.conference_record_sync import ConferenceRecordSyncService
from app.services.participant_sync import MeetingNotFoundError
from app.services.transcript_sync import TranscriptNotReadyError, TranscriptSyncService

logger = logging.getLogger(__name__)

_ALREADY_COMPLETED_MESSAGE = 'Meeting is already marked as completed. No changes made.'
_ENDED_SCHEDULED_MESSAGE = 'Meeting marked as ended. Official transcript sync scheduled.'
_ENDED_NO_TOKEN_MESSAGE = 'Meeting marked as ended. No Google access token provided; official transcript sync deferred.'
_ENDED_NO_RECORD_MESSAGE = ('Meeting marked as ended. No conference record available; '
                            'DOM transcript will serve as fallback source.')


class EndedService:
    def __init__(
        self,
        session: Session,
        sync_runner: Callable[[uuid.UUID, str, uuid.UUID, str], Any] | None = None,
    ) -> None:
        self.session = session
        self.meeting_repo = MeetingRepository(session)
        self.access_repo = MeetingAccessRepository(session)
        self.live_session_repo = LiveSessionRepository(session)
        self.sync_runner = sync_runner or self._default_sync_runner

    def mark_meeting_ended(
        self,
        meeting_id: uuid.UUID,
        user_id: uuid.UUID,
        google_access_token: str | None = None,
        background_tasks: Any | None = None,
    ) -> MeetingEndedResponse:
        """
        Mark a meeting as ended with 180s Debounce Grace Period logic:
        1. Access check: user must be meeting owner or participant in meeting_access.
        2. If other capture sessions are active (last_heartbeat within 90s), set 180s grace period.
        3. If all sessions disconnected, mark completed and schedule Official Transcript Sync.
        """
        # 1. Access check (Owner or granted access)
        meeting = self.meeting_repo.get_by_id(meeting_id)
        if meeting is None:
            raise MeetingNotFoundError(f'Meeting with id {meeting_id} not found.')

        user_role = self.access_repo.get_user_role(meeting_id, user_id)
        if meeting.user_id != user_id and user_role is None:
            raise MeetingNotFoundError(f'Meeting with id {meeting_id} not found.')

        # 2. Idempotency - already completed
        if meeting.status == 'completed':
            logger.info('Meeting already completed (idempotent): meeting_id=%s', meeting_id)
            return MeetingEndedResponse(
                id=meeting.id,
                status=meeting.status,
                transcript_status=meeting.transcript_status,
                message=_ALREADY_COMPLETED_MESSAGE,
            )

        # 3. Mark caller's active capture session(s) as COMPLETED
        self.live_session_repo.mark_user_sessions_completed(meeting_id, user_id)

        # 4. Check remaining active capture sessions
        active_session_count = self.live_session_repo.count_active_sessions_for_meeting(
            meeting_id, ttl_seconds=90
        )
        now = datetime.now(timezone.utc)

        if active_session_count > 0:
            # Active capture sessions still present -> Debounce Grace Period
            grace_expires = now + timedelta(seconds=180)
            self.meeting_repo.update(
                meeting_id,
                {"grace_period_expires_at": grace_expires},
            )
            logger.info(
                "Meeting %s has %d active session(s); entered 180s grace period until %s",
                meeting_id,
                active_session_count,
                grace_expires.isoformat(),
            )
            return MeetingEndedResponse(
                id=meeting.id,
                status=meeting.status,
                transcript_status=meeting.transcript_status,
                message=f"Session disconnected. Meeting remains active in grace period with {active_session_count} active capture session(s).",
            )

        # 4. Zero active sessions -> Mark meeting completed
        updated = self.meeting_repo.update(
            meeting_id,
            {'status': 'completed', 'transcript_status': 'processing', 'end_time': now},
        )
        if updated is None:
            raise MeetingNotFoundError(f'Meeting with id {meeting_id} not found.')

        logger.info(
            'Meeting ended: meeting_id=%s conference_record_name=%s has_token=%s',
            meeting_id,
            updated.conference_record_name,
            bool(google_access_token),
        )

        # 5. Schedule/Trigger Official Transcript Sync if conference record exists
        message = _ENDED_NO_RECORD_MESSAGE
        if updated.conference_record_name:
            if google_access_token:
                try:
                    if background_tasks is not None and hasattr(background_tasks, "add_task"):
                        background_tasks.add_task(
                            self.sync_runner,
                            meeting_id,
                            updated.conference_record_name,
                            user_id,
                            google_access_token,
                        )
                    else:
                        self.sync_runner(
                            meeting_id,
                            updated.conference_record_name,
                            user_id,
                            google_access_token,
                        )
                    message = _ENDED_SCHEDULED_MESSAGE
                except Exception as exc:
                    logger.warning('Official transcript sync scheduling failed: meeting_id=%s err=%s', meeting_id, exc)
                    message = ('Meeting marked as ended. Official transcript sync could not be scheduled; '
                               'DOM transcript will serve as fallback source.')
            else:
                message = _ENDED_NO_TOKEN_MESSAGE

        return MeetingEndedResponse(
            id=updated.id,
            status=updated.status,
            transcript_status=updated.transcript_status,
            message=message,
        )

    def _default_sync_runner(
        self,
        meeting_id: uuid.UUID,
        conference_record_name: str,
        user_id: uuid.UUID,
        access_token: str,
    ) -> None:
        client = GoogleMeetClient(access_token=access_token)
        try:
            conf_service = ConferenceRecordSyncService(self.meeting_repo, client)
            conf_service.sync_conference_record(conference_record_name, user_id)
        except Exception as exc:
            logger.warning("Conference record sync warning in ended trigger: %s", exc)

        tx_service = TranscriptSyncService(
            transcript_repository=TranscriptRepository(self.session),
            transcript_entry_repository=TranscriptEntryRepository(self.session),
            participant_repository=ParticipantRepository(self.session),
            google_meet_client=client,
            meeting_repository=self.meeting_repo,
            session=self.session,
        )
        try:
            tx_service.sync_transcripts(conference_record_name, meeting_id)
            self.meeting_repo.update(meeting_id, {"transcript_status": "available"})
            logger.info("Official transcript sync completed for meeting %s", meeting_id)
        except TranscriptNotReadyError:
            logger.info("Official transcript not ready yet for meeting %s (remains processing)", meeting_id)
            self.meeting_repo.update(meeting_id, {"transcript_status": "processing"})
        except Exception as exc:
            logger.warning("Transcript sync warning in ended trigger: %s", exc)
