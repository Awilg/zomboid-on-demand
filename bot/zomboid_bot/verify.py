from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey


def is_valid(public_key_hex: str, signature_hex: str, timestamp: str, body: str) -> bool:
    try:
        VerifyKey(bytes.fromhex(public_key_hex)).verify(
            timestamp.encode() + body.encode(), bytes.fromhex(signature_hex)
        )
    except (BadSignatureError, ValueError, TypeError):
        return False
    return True
