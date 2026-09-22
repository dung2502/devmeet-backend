import re
import uuid
from datetime import datetime
from typing import Literal

from sqlalchemy.orm import Session

from app.models import Meeting
from app.services.participant_sync import MeetingNotFoundError
from app.services.transcript_source_selector import TranscriptSourceSelector
from app.services.transcript_view_service import TranscriptViewService

import unicodedata

ExportType = Literal["transcript", "summary", "all"]


def sanitize_filename(name: str) -> str:
    """Sanitize string to be safe for filenames across OS platforms and HTTP ASCII headers."""
    ascii_name = (
        unicodedata.normalize("NFKD", name)
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    clean = re.sub(r'[^a-zA-Z0-9_\-\. ]', "", ascii_name)
    clean = clean.replace(" ", "_").strip("._-")
    return clean[:50] if clean else "Meeting"


class ExportService:
    """Service to export Meeting Transcripts, AI Summaries, or Combined Reports as plain text UTF-8 files."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.selector = TranscriptSourceSelector(session)
        self.view_service = TranscriptViewService(session)

    def export_meeting(
        self,
        meeting_id: uuid.UUID,
        export_type: ExportType = "transcript",
        user_id: uuid.UUID | None = None,
    ) -> tuple[str, str]:
        """
        Generates formatted plain-text content and a safe filename for export.
        Returns: (content_str, filename_str)
        """
        meeting = self.session.get(Meeting, meeting_id)
        if meeting is None:
            raise MeetingNotFoundError(f"Meeting with id {meeting_id} not found.")

        if user_id is not None and meeting.user_id != user_id:
            raise MeetingNotFoundError(f"Meeting with id {meeting_id} not found.")

        title = meeting.title or "Cuộc họp"
        safe_title = sanitize_filename(title)
        date_str = (
            meeting.start_time.strftime("%Y-%m-%d")
            if meeting.start_time
            else datetime.utcnow().strftime("%Y-%m-%d")
        )

        if export_type == "transcript":
            content = self._format_transcript(meeting_id, meeting)
            filename = f"Transcript_{safe_title}_{date_str}.txt"
            return content, filename

        if export_type == "summary":
            content = self._format_summary(meeting)
            filename = f"AI_Summary_{safe_title}_{date_str}.txt"
            return content, filename

        if export_type == "all":
            content = self._format_all(meeting_id, meeting)
            filename = f"Full_Meeting_Report_{safe_title}_{date_str}.txt"
            return content, filename

        raise ValueError(f"Invalid export type: {export_type}")

    def _format_transcript(self, meeting_id: uuid.UUID, meeting: Meeting) -> str:
        view = self.view_service.get_transcript_view(meeting_id)
        lines = [
            f"DEVMEETING AI — TRANSCRIPT",
            f"==========================",
            f"Cuộc họp: {meeting.title or 'N/A'}",
            f"Thời gian: {meeting.start_time.strftime('%Y-%m-%d %H:%M:%S') if meeting.start_time else 'N/A'}",
            f"Nguồn: {view.source} (Tổng {view.total_entries} câu thoại)",
            f"--------------------------------------------------\n",
        ]

        if not view.entries:
            lines.append("Không có dữ liệu transcript cho cuộc họp này.")
        else:
            for entry in view.entries:
                lines.append(f"[{entry.timestamp}] {entry.speaker}: {entry.text}")

        return "\n".join(lines)

    def _format_summary(self, meeting: Meeting) -> str:
        ai_result = meeting.ai_result or {}
        summary = ai_result.get("summary") or "Chưa có bản tóm tắt AI."
        key_points = ai_result.get("key_points") or []
        decisions = ai_result.get("decisions") or []
        action_items = ai_result.get("action_items") or []
        email = ai_result.get("follow_up_email") or {}

        lines = [
            "DEVMEETING AI — TỔNG KẾT VÀ HÀNH ĐỘNG",
            "====================================",
            f"Cuộc họp: {meeting.title or 'N/A'}",
            f"Thời gian: {meeting.start_time.strftime('%Y-%m-%d %H:%M:%S') if meeting.start_time else 'N/A'}",
            f"Trạng thái AI: {meeting.ai_status}",
            "--------------------------------------------------\n",
            "1. TÓM TẮT CUỘC HỌP (SUMMARY)",
            "-----------------------------",
            summary,
            "",
        ]

        if key_points:
            lines.append("2. CÁC ĐIỂM MẤU CHỐT (KEY POINTS)")
            lines.append("---------------------------------")
            for kp in key_points:
                lines.append(f"- {kp}")
            lines.append("")

        if decisions:
            lines.append(f"3. QUYẾT ĐỊNH ĐÃ THỐNG NHẤT ({len(decisions)})")
            lines.append("--------------------------------------")
            for idx, d in enumerate(decisions, 1):
                dec_text = d.get("decision", "")
                ctx = d.get("context", "")
                ts = d.get("evidence_timestamp", "")
                ts_str = f" [Bằng chứng: {ts}]" if ts else ""
                lines.append(f"{idx}. {dec_text}{ts_str}")
                if ctx:
                    lines.append(f"   Bối cảnh: {ctx}")
            lines.append("")

        if action_items:
            lines.append(f"4. CÔNG VIỆC CẦN LÀM / ACTION ITEMS ({len(action_items)})")
            lines.append("-------------------------------------------------")
            for idx, a in enumerate(action_items, 1):
                task = a.get("task", "")
                assignee = a.get("assignee", "Chưa phân công")
                deadline = a.get("deadline") or a.get("due_date") or "Không có hạn"
                status_str = a.get("status", "TODO")
                ts = a.get("evidence_timestamp", "")
                ts_str = f" [Bằng chứng: {ts}]" if ts else ""
                lines.append(f"{idx}. {task}")
                lines.append(f"   Người phụ trách: {assignee} | Hạn chót: {deadline} | Trạng thái: {status_str}{ts_str}")
            lines.append("")

        if email:
            lines.append("5. EMAIL TỔNG KẾT (FOLLOW-UP EMAIL)")
            lines.append("-----------------------------------")
            lines.append(f"Tiêu đề: {email.get('subject', 'N/A')}")
            lines.append("")
            lines.append(email.get("body", ""))
            lines.append("")

        return "\n".join(lines)

    def _format_all(self, meeting_id: uuid.UUID, meeting: Meeting) -> str:
        summary_part = self._format_summary(meeting)
        transcript_part = self._format_transcript(meeting_id, meeting)

        return (
            summary_part
            + "\n\n"
            + "==================================================\n"
            + "CHI TIẾT TOÀN BỘ TRANSCRIPT CUỘC HỌP\n"
            + "==================================================\n\n"
            + transcript_part
        )
