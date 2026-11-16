from fastapi.testclient import TestClient

import server


def test_instant_feedback_saves_once_and_counts_in_rating(mongo):
    """The Service-page smiley posts to the same one-time /feedback/{token} as the WhatsApp link."""
    db, run = mongo
    jid = str(run(db.service_jobs.insert_one({"job_number": "SRV-000009", "status": "delivered"})).inserted_id)
    token = run(server._ensure_feedback_token(jid))
    c = TestClient(server.app)
    assert c.post(f"/api/feedback/{token}", json={"rating": 3}).status_code == 200
    assert c.post(f"/api/feedback/{token}", json={"rating": 5}).status_code == 409   # customer can't overwrite it
    assert run(db.service_jobs.find_one({}))["feedback_rating"] == 3

