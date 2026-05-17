import unittest

from remote_explorer_server.protocol import make_request_id
from remote_explorer_server.security import (
    AuthManager,
    derive_password_key,
    hmac_hex,
    proof_message,
    session_message,
    sign_message,
)


class SecurityTests(unittest.TestCase):
    def test_password_auth_and_signed_command(self) -> None:
        auth = AuthManager("secret")
        client = {"id": "client-1", "name": "Test Client"}

        challenge_response = auth.start_auth(client)
        challenge = challenge_response["challenge"]
        client_nonce = "abcd"
        key = derive_password_key("secret", challenge["salt"], challenge["iterations"])
        proof = hmac_hex(
            key,
            proof_message(client["id"], challenge["server_nonce"], client_nonce),
        )

        auth_ok = auth.complete_auth(
            {
                "client_id": client["id"],
                "server_nonce": challenge["server_nonce"],
                "client_nonce": client_nonce,
                "proof": proof,
            }
        )
        session_id = auth_ok["session"]["id"]
        session_key = hmac_hex(
            key,
            session_message(client["id"], challenge["server_nonce"], client_nonce),
        )
        command = {
            "v": 1,
            "type": "command",
            "request_id": make_request_id(),
            "command": "status",
            "payload": {},
            "auth": {
                "session_id": session_id,
                "counter": 1,
            },
        }
        command["auth"]["signature"] = sign_message(command, bytes.fromhex(session_key))

        session = auth.verify_command(command)

        self.assertEqual(session.id, session_id)
        self.assertEqual(session.client_id, client["id"])

    def test_no_password_session(self) -> None:
        auth = AuthManager(None)
        response = auth.start_auth({"id": "client-1"})

        command = {
            "v": 1,
            "type": "command",
            "request_id": "cmd",
            "command": "status",
            "auth": {
                "session_id": response["session"]["id"],
            },
        }

        self.assertEqual(auth.verify_command(command).mode, "none")


if __name__ == "__main__":
    unittest.main()
