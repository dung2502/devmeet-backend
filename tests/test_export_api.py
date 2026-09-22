import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.main import app
from app.repositories import MeetingAccessRepository, MeetingRepository, UserRepository


@pytest.fixture()
def auth_setup(db_session: Session):
    user_repo = UserRepository(db_session)
    user_a = user_repo.create(
        {
            "google_user_id": "google-user-exp-111",
            "email": "user_exp_a@example.com",
            "display_name": "User Exp Alpha",
        }
    )
    user_b = user_repo.create(
        {
            "google_user_id": "google-user-exp-222",
            "email": "user_exp_b@example.com",
            "display_name": "User Exp Beta",
        }
    )

    current_active_user = [user_a]

    def override_get_db():
        yield db_session

    def override_get_current_user():
        return current_active_user[0]

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user

    with TestClient(app) as client:
        yield {
            "client": client,
            "user_a": user_a,
            "user_b": user_b,
            "set_user": lambda u: current_active_user.__setitem__(0, u),
            "session": db_session,
        }

    app.dependency_overrides.clear()


def test_export_transcript_txt(auth_setup):
    client = auth_setup["client"]
    user_a = auth_setup["user_a"]
    session = auth_setup["session"]

    m = MeetingRepository(session).create(
        {
            "user_id": user_a.id,
            "title": "Cuộc họp Kiểm thử Q3",
            "start_time": datetime(2026, 9, 15, 14, 30, tzinfo=timezone.utc),
            "dom_capture_status": "captured",
            "dom_transcript_data": [
                {"speaker": "Nguyen Van A", "text": "Chung ta bat dau cuoc hop.", "start_time": "00:01:04"},
                {"speaker": "Tran Thi B", "text": "Toi nhat tri voi phuong an.", "start_time": "00:02:11"},
            ],
        }
    )

    res = client.get(f"/api/v1/meetings/{m.id}/export?type=transcript")
    assert res.status_code == 200
    assert "text/plain" in res.headers["content-type"]
    assert "charset=utf-8" in res.headers["content-type"]
    assert "attachment; filename=" in res.headers["content-disposition"]
    assert "Transcript_" in res.headers["content-disposition"]

    text_body = res.text
    assert "DEVMEETING AI — TRANSCRIPT" in text_body
    assert "[00:01:04] Nguyen Van A: Chung ta bat dau cuoc hop." in text_body
    assert "[00:02:11] Tran Thi B: Toi nhat tri voi phuong an." in text_body


def test_export_summary_txt(auth_setup):
    client = auth_setup["client"]
    user_a = auth_setup["user_a"]
    session = auth_setup["session"]

    ai_data = {
        "summary": "Cuộc họp thống nhất kế hoạch triển khai Phase 6.",
        "key_points": ["Điểm 1: Hoàn thành backend", "Điểm 2: Xây dựng web dashboard"],
        "decisions": [
            {"decision": "Sử dụng Vite React cho Web", "context": "Đã được phê duyệt", "evidence_timestamp": "00:05:12"}
        ],
        "action_items": [
            {
                "task": "Viết unit test",
                "assignee": "Nguyen",
                "deadline": "2026-09-16",
                "status": "TODO",
                "evidence_timestamp": "00:10:00",
            }
        ],
        "follow_up_email": {
            "subject": "Tổng kết cuộc họp Phase 6",
            "body": "Chào team,\n\nDưới đây là tổng kết...",
        },
    }

    m = MeetingRepository(session).create(
        {
            "user_id": user_a.id,
            "title": "Kế hoạch Phase 6",
            "start_time": datetime(2026, 9, 15, 15, 0, tzinfo=timezone.utc),
            "ai_status": "COMPLETED",
            "ai_result": ai_data,
        }
    )

    res = client.get(f"/api/v1/meetings/{m.id}/export?type=summary")
    assert res.status_code == 200
    assert "AI_Summary_" in res.headers["content-disposition"]
    text_body = res.text

    assert "1. TÓM TẮT CUỘC HỌP (SUMMARY)" in text_body
    assert "Cuộc họp thống nhất kế hoạch triển khai Phase 6." in text_body
    assert "2. CÁC ĐIỂM MẤU CHỐT (KEY POINTS)" in text_body
    assert "- Điểm 1: Hoàn thành backend" in text_body
    assert "3. QUYẾT ĐỊNH ĐÃ THỐNG NHẤT" in text_body
    assert "Sử dụng Vite React cho Web [Bằng chứng: 00:05:12]" in text_body
    assert "4. CÔNG VIỆC CẦN LÀM / ACTION ITEMS" in text_body
    assert "Viết unit test" in text_body
    assert "5. EMAIL TỔNG KẾT (FOLLOW-UP EMAIL)" in text_body
    assert "Tiêu đề: Tổng kết cuộc họp Phase 6" in text_body


def test_export_all_txt(auth_setup):
    client = auth_setup["client"]
    user_a = auth_setup["user_a"]
    session = auth_setup["session"]

    m = MeetingRepository(session).create(
        {
            "user_id": user_a.id,
            "title": "Báo cáo tổng hợp",
            "start_time": datetime(2026, 9, 15, 16, 0, tzinfo=timezone.utc),
            "ai_status": "COMPLETED",
            "ai_result": {"summary": "Tóm tắt ngắn gọn."},
            "dom_capture_status": "captured",
            "dom_transcript_data": [{"speaker": "Lead", "text": "Noi dung.", "start_time": "00:00:10"}],
        }
    )

    res = client.get(f"/api/v1/meetings/{m.id}/export?type=all")
    assert res.status_code == 200
    assert "Full_Meeting_Report_" in res.headers["content-disposition"]
    text_body = res.text

    assert "Tóm tắt ngắn gọn." in text_body
    assert "CHI TIẾT TOÀN BỘ TRANSCRIPT CUỘC HỌP" in text_body
    assert "[00:00:10] Lead: Noi dung." in text_body


def test_export_user_isolation(auth_setup):
    client = auth_setup["client"]
    user_b = auth_setup["user_b"]
    session = auth_setup["session"]

    m_b = MeetingRepository(session).create(
        {
            "user_id": user_b.id,
            "title": "Meeting of User B",
        }
    )

    res = client.get(f"/api/v1/meetings/{m_b.id}/export?type=transcript")
    assert res.status_code == 404


def test_export_participant_allowed(auth_setup):
    client = auth_setup["client"]
    user_a = auth_setup["user_a"]
    user_b = auth_setup["user_b"]
    set_user = auth_setup["set_user"]
    session = auth_setup["session"]

    # User A creates meeting
    m = MeetingRepository(session).create(
        {
            "user_id": user_a.id,
            "title": "Cuộc họp Nhóm",
            "start_time": datetime(2026, 9, 15, 14, 30, tzinfo=timezone.utc),
            "dom_capture_status": "captured",
            "dom_transcript_data": [
                {"speaker": "Nguyen Van A", "text": "Chung ta bat dau cuoc hop.", "start_time": "00:01:04"},
            ],
            "ai_status": "COMPLETED",
            "ai_result": {"summary": "Tóm tắt cuộc họp nhóm."},
        }
    )

    # Grant User B participant access
    MeetingAccessRepository(session).add_or_update_access(m.id, user_b.id, role="PARTICIPANT")
    session.commit()

    # Switch current user to User B (participant / "Tôi tham gia")
    set_user(user_b)

    # Participant exporting transcript
    res_transcript = client.get(f"/api/v1/meetings/{m.id}/export?type=transcript")
    assert res_transcript.status_code == 200
    assert "DEVMEETING AI — TRANSCRIPT" in res_transcript.text
    assert "Nguyen Van A: Chung ta bat dau cuoc hop." in res_transcript.text

    # Participant exporting summary
    res_summary = client.get(f"/api/v1/meetings/{m.id}/export?type=summary")
    assert res_summary.status_code == 200
    assert "Tóm tắt cuộc họp nhóm." in res_summary.text

    # Participant exporting all
    res_all = client.get(f"/api/v1/meetings/{m.id}/export?type=all")
    assert res_all.status_code == 200
    assert "Tóm tắt cuộc họp nhóm." in res_all.text

