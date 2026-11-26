from __future__ import annotations

import socket
import tempfile
import unittest
from email import policy
from email.parser import BytesParser
from pathlib import Path

from secret_gift.mail import MailMessage, SshSendmailService


class FakeAuthenticationException(Exception):
    pass


class FakeBadHostKeyException(Exception):
    pass


class FakeSSHException(Exception):
    pass


class FakeRejectPolicy:
    pass


class FakeStdin:
    def __init__(self):
        self.data = b""
        self.flushed = False
        self.shutdown = False
        self.closed = False
        self.channel = self

    def write(self, data):
        self.data += data

    def flush(self):
        self.flushed = True

    def shutdown_write(self):
        self.shutdown = True

    def close(self):
        self.closed = True


class FakeChannel:
    def __init__(self, exit_code=0, error=None):
        self.exit_code = exit_code
        self.error = error

    def recv_exit_status(self):
        if self.error:
            raise self.error
        return self.exit_code


class FakeStdout:
    def __init__(self, exit_code=0, error=None):
        self.channel = FakeChannel(exit_code, error)


class FakeStderr:
    def __init__(self, content=b""):
        self.content = content
        self.read_size = None

    def read(self, size=-1):
        self.read_size = size
        return self.content[:size]


class FakeClient:
    def __init__(self, *, connect_error=None, exec_error=None, exit_code=0, wait_error=None, stderr=b""):
        self.connect_error = connect_error
        self.exec_error = exec_error
        self.exit_code = exit_code
        self.wait_error = wait_error
        self.stderr_content = stderr
        self.loaded_hosts = ""
        self.policy = None
        self.connect_kwargs = None
        self.command = ""
        self.command_timeout = None
        self.closed = False
        self.stdin = None
        self.stderr = None

    def load_host_keys(self, path):
        self.loaded_hosts = path

    def set_missing_host_key_policy(self, policy):
        self.policy = policy

    def connect(self, **kwargs):
        self.connect_kwargs = kwargs
        if self.connect_error:
            raise self.connect_error

    def exec_command(self, command, timeout):
        self.command = command
        self.command_timeout = timeout
        if self.exec_error:
            raise self.exec_error
        self.stdin = FakeStdin()
        self.stderr = FakeStderr(self.stderr_content)
        return self.stdin, FakeStdout(self.exit_code, self.wait_error), self.stderr

    def close(self):
        self.closed = True


class FakeParamiko:
    AuthenticationException = FakeAuthenticationException
    BadHostKeyException = FakeBadHostKeyException
    SSHException = FakeSSHException
    RejectPolicy = FakeRejectPolicy

    def __init__(self, client):
        self.client = client

    def SSHClient(self):
        return self.client


def mail_message(recipient="person@claro.com.pe"):
    return MailMessage(
        to=recipient,
        subject="Tu amigo secreto de Año Nuevo",
        html="<html><body>Hola</body></html>",
        text="Hola",
        idempotency_key="event:draw:participant",
    )


class SshSendmailTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.password_file = self.root / "password.txt"
        self.known_hosts = self.root / "known_hosts"
        self.password_file.write_text("private-password\n", encoding="utf-8")
        self.password_file.chmod(0o600)
        self.known_hosts.write_text("172.19.30.62 ssh-ed25519 AAAATEST\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def service(self, client=None, *, test_mode=False, test_recipient="test@claro.com.pe"):
        client = client or FakeClient()
        return SshSendmailService(
            host="172.19.30.62",
            port=22,
            username="usr_mds",
            password_file=self.password_file,
            known_hosts_file=self.known_hosts,
            from_address="amigo-secreto@claro.com.pe",
            from_name="Amigo secreto de Año Nuevo",
            timeout=30,
            test_mode=test_mode,
            test_recipient=test_recipient,
            paramiko_module=FakeParamiko(client),
        ), client

    def test_successful_connection_uses_pinned_hosts_and_closes(self):
        service, client = self.service()
        result = service.test_connection()
        self.assertEqual("accepted", result.status)
        self.assertEqual(str(self.known_hosts), client.loaded_hosts)
        self.assertIsInstance(client.policy, FakeRejectPolicy)
        self.assertFalse(client.connect_kwargs["allow_agent"])
        self.assertFalse(client.connect_kwargs["look_for_keys"])
        self.assertEqual("private-password", client.connect_kwargs["password"])
        self.assertTrue(client.closed)

    def test_missing_and_empty_password_file_are_rejected(self):
        self.password_file.unlink()
        service, _ = self.service()
        with self.assertRaisesRegex(ValueError, "Falta el archivo"):
            service.validate_configuration()
        self.password_file.write_text("\n", encoding="utf-8")
        self.password_file.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "está vacío"):
            service.validate_configuration()

    def test_missing_known_hosts_is_rejected(self):
        self.known_hosts.unlink()
        service, _ = self.service()
        with self.assertRaisesRegex(ValueError, "known_hosts"):
            service.validate_configuration()

    def test_unknown_or_changed_host_key_is_rejected(self):
        for error, expected in (
            (FakeSSHException("unknown host"), "ssh_security_error"),
            (FakeBadHostKeyException("changed"), "host_key_mismatch"),
        ):
            with self.subTest(expected=expected):
                service, client = self.service(FakeClient(connect_error=error))
                result = service.test_connection()
                self.assertEqual("failed", result.status)
                self.assertEqual(expected, result.error_type)
                self.assertTrue(client.closed)

    def test_authentication_rejection_and_timeout(self):
        for error, expected in (
            (FakeAuthenticationException("bad password"), "authentication_rejected"),
            (socket.timeout(), "connection_timeout"),
        ):
            with self.subTest(expected=expected):
                service, _ = self.service(FakeClient(connect_error=error))
                result = service.test_connection()
                self.assertEqual("failed", result.status)
                self.assertEqual(expected, result.error_type)

    def test_sendmail_accepts_only_exit_zero_and_closes_stdin(self):
        service, client = self.service(FakeClient(exit_code=0))
        result = service.send(mail_message())
        self.assertEqual("accepted", result.status)
        self.assertEqual(0, result.exit_code)
        self.assertEqual("sendmail -i person@claro.com.pe", client.command)
        self.assertTrue(client.stdin.flushed)
        self.assertTrue(client.stdin.shutdown)
        self.assertTrue(client.stdin.closed)
        self.assertTrue(client.closed)
        parsed = BytesParser(policy=policy.default).parsebytes(client.stdin.data)
        self.assertEqual("amigo-secreto@claro.com.pe", parsed["From"].addresses[0].addr_spec)
        self.assertEqual("event:draw:participant", parsed["X-Idempotency-Key"])
        self.assertTrue(parsed.is_multipart())

    def test_nonzero_exit_is_failed_and_stderr_is_limited(self):
        service, client = self.service(FakeClient(exit_code=75, stderr=b"temporary failure" * 300))
        result = service.send(mail_message())
        self.assertEqual("failed", result.status)
        self.assertEqual(75, result.exit_code)
        self.assertEqual("sendmail_exit", result.error_type)
        self.assertEqual(service.STDERR_LIMIT + 1, client.stderr.read_size)
        self.assertLessEqual(len(result.technical_error), 350)

    def test_uncertain_remote_result_is_never_accepted(self):
        service, _ = self.service(FakeClient(wait_error=socket.timeout()))
        result = service.send(mail_message())
        self.assertEqual("unknown", result.status)
        self.assertEqual("remote_result_unknown", result.error_type)

    def test_invalid_and_injection_recipients_are_rejected_before_ssh(self):
        for address in ("invalid", "victim@claro.com.pe; touch /tmp/x", "a@b.com\nBcc:x@y.com"):
            with self.subTest(address=address):
                service, client = self.service()
                result = service.send(mail_message(address))
                self.assertEqual("failed", result.status)
                self.assertEqual("invalid_recipient", result.error_type)
                self.assertIsNone(client.connect_kwargs)

    def test_test_mode_replaces_recipient_and_marks_original_in_subject(self):
        service, client = self.service(test_mode=True, test_recipient="control@claro.com.pe")
        result = service.send(mail_message("original@claro.com.pe"))
        self.assertEqual("accepted", result.status)
        self.assertEqual("sendmail -i control@claro.com.pe", client.command)
        parsed = BytesParser(policy=policy.default).parsebytes(client.stdin.data)
        self.assertEqual("control@claro.com.pe", parsed["To"].addresses[0].addr_spec)
        self.assertIn("destinatario original: original@claro.com.pe", str(parsed["Subject"]))


if __name__ == "__main__":
    unittest.main()
