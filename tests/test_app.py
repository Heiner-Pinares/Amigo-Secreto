from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlencode

from app import Application, parse_csv_participants
from secret_gift import db
from secret_gift.drawing import DrawError, DrawParticipant, generate_draw, validate_draw
from secret_gift.mail import MailResult, render_assignment_mail


class FakeMailService:
    def __init__(self, statuses=None):
        self.calls = []
        self.statuses = list(statuses or [])

    def send(self, message):
        self.calls.append(message)
        status = self.statuses.pop(0) if self.statuses else "accepted"
        if status == "timeout":
            raise TimeoutError("provider timeout")
        return MailResult(status=status, reference="ref-test", technical_error="mock error" if status == "failed" else "")


class WsgiClient:
    def __init__(self, app):
        self.app = app
        self.cookie = ""

    def request(self, path, method="GET", data=None):
        raw = urlencode(data or {}).encode()
        captured = {}

        def start_response(status, headers):
            captured["status"] = int(status.split()[0])
            captured["headers"] = headers
            for key, value in headers:
                if key.lower() == "set-cookie":
                    self.cookie = value.split(";", 1)[0]

        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "CONTENT_LENGTH": str(len(raw)),
            "CONTENT_TYPE": "application/x-www-form-urlencoded",
            "HTTP_COOKIE": self.cookie,
            "wsgi.input": io.BytesIO(raw),
        }
        body = b"".join(self.app(environ, start_response)).decode()
        return captured["status"], body, captured["headers"]

    def csrf(self):
        sid = self.cookie.split("=", 1)[1]
        return self.app.sessions[sid]["csrf"]

    def login(self):
        self.request("/")
        status, _, _ = self.request("/admin/login", "POST", {"csrf": self.csrf(), "password": "test-password"})
        if status != 303:
            raise AssertionError("Login failed")


class DrawTests(unittest.TestCase):
    def test_complete_draws_for_required_sizes(self):
        for size in (2, 3, 10, 100):
            with self.subTest(size=size):
                people = [DrawParticipant(str(i), f"area-{i % 8}") for i in range(size)]
                result = generate_draw(people)
                validate_draw(result, {person.id for person in people})
                self.assertEqual(size, len(result))
                self.assertEqual(size, len(set(result.values())))
                self.assertFalse(any(giver == recipient for giver, recipient in result.items()))

    def test_insufficient_participants(self):
        with self.assertRaises(DrawError):
            generate_draw([DrawParticipant("only")])

    def test_impossible_restrictions(self):
        people = [DrawParticipant("a"), DrawParticipant("b")]
        with self.assertRaises(DrawError):
            generate_draw(people, {("a", "b")})

    def test_avoid_same_area_can_be_impossible(self):
        people = [DrawParticipant("a", "sales"), DrawParticipant("b", "sales")]
        with self.assertRaises(DrawError):
            generate_draw(people, avoid_same_area=True)


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "test.sqlite3"
        self.event_id = db.initialize(self.path, "Evento de prueba")

    def tearDown(self):
        self.temp.cleanup()

    def add_people(self, count):
        with db.connect(self.path) as conn:
            return [db.add_participant(conn, self.event_id, name=f"Persona {i}", email=f"p{i}@example.test", gifts=f"Regalo {i}") for i in range(count)]

    def test_duplicate_email_rejected_after_normalization(self):
        with db.connect(self.path) as conn:
            db.add_participant(conn, self.event_id, name="Persona Uno", email="TEST@Example.test ", gifts="Un libro")
            with self.assertRaisesRegex(ValueError, "ya está registrado"):
                db.add_participant(conn, self.event_id, name="Persona Dos", email="test@example.test", gifts="Un juego")

    def test_confirmation_rolls_back_completely(self):
        ids = self.add_people(3)
        assignments = {ids[0]: ids[1], ids[1]: ids[2], ids[2]: ids[0]}
        with db.connect(self.path) as conn:
            db.save_preview(conn, self.event_id, assignments, "digest", "test")
            with self.assertRaises(RuntimeError):
                db.confirm_preview(conn, self.event_id, "test", fail_after=1)
            count = conn.execute("SELECT COUNT(*) n FROM assignments").fetchone()["n"]
            event = conn.execute("SELECT status FROM events WHERE id=?", (self.event_id,)).fetchone()
        self.assertEqual(0, count)
        self.assertEqual("preview", event["status"])

    def test_csv_import_detects_duplicates_and_invalid_rows(self):
        raw = "nombre,correo,regalos,area,confirmado\nAna,a@example.test,Libro,Ventas,sí\nAna 2,a@example.test,Taza,TI,sí\nLuis,l@example.test,,TI,sí\nEva,e@example.test,Dulces,TI,no\n"
        valid, invalid = parse_csv_participants(raw, set())
        self.assertEqual(1, len(valid))
        self.assertEqual(3, len(invalid))


class WebSecurityAndMailTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.mail = FakeMailService()
        self.app = Application(Path(self.temp.name) / "web.sqlite3", "test-password", mail_service=self.mail, mail_enabled=True)
        self.client = WsgiClient(self.app)

    def tearDown(self):
        self.temp.cleanup()

    def seed_confirmed_draw(self, count=3):
        with db.connect(self.app.database_path) as conn:
            ids = [db.add_participant(conn, self.app.event_id, name=f"Persona {i}", email=f"person{i}@example.test", gifts=f"Regalo {i}") for i in range(count)]
            assignments = {ids[i]: ids[(i + 1) % count] for i in range(count)}
            db.save_preview(conn, self.app.event_id, assignments, "digest", "test")
            db.confirm_preview(conn, self.app.event_id, "test")
        return ids

    def test_admin_authorization_and_csrf(self):
        self.client.request("/")
        status, _, _ = self.client.request("/admin/draw/preview", "POST", {"csrf": self.client.csrf()})
        self.assertEqual(401, status)
        self.client.login()
        status, body, _ = self.client.request("/admin/event", "POST", {"csrf": "wrong"})
        self.assertEqual(403, status)
        self.assertIn("CSRF", body)

    def test_credentials_are_not_exposed_to_browser(self):
        status, body, _ = self.client.request("/")
        self.assertEqual(200, status)
        self.assertNotIn("test-password", body)
        self.assertNotIn("SECRET_GIFT_MAIL_TOKEN", body)
        self.assertNotIn("SECRET_GIFT_MAIL_URL", body)

    def test_preview_does_not_send_mail(self):
        with db.connect(self.app.database_path) as conn:
            for index in range(3):
                db.add_participant(conn, self.app.event_id, name=f"Persona {index}", email=f"p{index}@example.test", gifts="Libro")
        self.client.login()
        status, body, _ = self.client.request("/admin/draw/preview", "POST", {"csrf": self.client.csrf()})
        self.assertEqual(200, status)
        self.assertIn("No se enviaron correos", body)
        self.assertEqual([], self.mail.calls)

    def test_double_send_does_not_duplicate_mail(self):
        self.seed_confirmed_draw(3)
        self.client.login()
        csrf = self.client.csrf()
        first, _, _ = self.client.request("/admin/mail/send", "POST", {"csrf": csrf})
        second, _, _ = self.client.request("/admin/mail/send", "POST", {"csrf": csrf})
        self.assertEqual(200, first)
        self.assertEqual(200, second)
        self.assertEqual(3, len(self.mail.calls))
        self.assertEqual(3, len({call.idempotency_key for call in self.mail.calls}))

    def test_retry_only_failed(self):
        self.mail.statuses = ["accepted", "failed", "accepted"]
        self.seed_confirmed_draw(3)
        self.client.login()
        csrf = self.client.csrf()
        self.client.request("/admin/mail/send", "POST", {"csrf": csrf})
        self.client.request("/admin/mail/retry", "POST", {"csrf": csrf})
        self.assertEqual(4, len(self.mail.calls))
        self.assertEqual(self.mail.calls[1].idempotency_key, self.mail.calls[3].idempotency_key)

    def test_timeout_is_unknown_and_not_retried_as_failed(self):
        self.mail.statuses = ["timeout", "accepted"]
        self.seed_confirmed_draw(2)
        self.client.login()
        csrf = self.client.csrf()
        self.client.request("/admin/mail/send", "POST", {"csrf": csrf})
        self.client.request("/admin/mail/retry", "POST", {"csrf": csrf})
        self.assertEqual(2, len(self.mail.calls))
        with db.connect(self.app.database_path) as conn:
            statuses = {row["mail_status"] for row in conn.execute("SELECT mail_status FROM assignments")}
        self.assertEqual({"unknown", "accepted"}, statuses)

    def test_html_mail_escapes_user_content(self):
        message = render_assignment_mail(giver_name="<Admin>", recipient_name="<script>alert(1)</script>", recipient_gifts="Libro & taza", event_name="Evento", instructions="No <b>HTML</b>", event_date="", event_time="", location="", budget="", idempotency_key="key", to="x@example.test")
        self.assertNotIn("<script>", message.html)
        self.assertNotIn("<b>HTML</b>", message.html)
        self.assertIn("&lt;script&gt;", message.html)
        self.assertIn("Libro &amp; taza", message.html)


if __name__ == "__main__":
    unittest.main()
