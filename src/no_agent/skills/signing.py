"""
技能签名（Ed25519）

签名覆盖技能目录下每个文件的 SHA-256（签名文件本身除外），任何文件被改动签名即失效。

    no-agent keygen --out team.key            # 生成私钥，打印公钥
    no-agent sign path/to/skill --key team.key --signer team
    AgentConfig(trusted_signers={"team": "<公钥>"})

需要 pip install "No-Agent[sign]"（cryptography）。没有安装时，所有共享技能都按"未签名"处理。
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

SIGNATURE_FILE = "no-agent.sig.json"
_SKIP_DIRS = {"__pycache__", ".git"}


def file_digests(d: Path) -> dict[str, str]:
    out = {}
    for p in sorted(d.rglob("*")):
        if not p.is_file() or p.name == SIGNATURE_FILE or p.suffix == ".pyc" or _SKIP_DIRS & set(p.relative_to(d).parts):
            continue
        out[p.relative_to(d).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _payload(skill_id: str, digests: dict[str, str]) -> bytes:
    return json.dumps({"skill": skill_id, "files": digests}, sort_keys=True, separators=(",", ":")).encode("utf-8")


def generate_keypair() -> tuple[bytes, str]:
    """返回 (私钥 PEM, 公钥 base64)"""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return pem, base64.b64encode(pub).decode()


def sign_skill(d: Path, private_pem: bytes, signer: str) -> dict[str, Any]:
    from cryptography.hazmat.primitives import serialization

    key = serialization.load_pem_private_key(private_pem, password=None)
    digests = file_digests(d)
    sig = {
        "signer": signer,
        "algorithm": "ed25519",
        "skill": d.name,
        "files": digests,
        "signature": base64.b64encode(key.sign(_payload(d.name, digests))).decode(),  # type: ignore[call-arg]
    }
    (d / SIGNATURE_FILE).write_text(json.dumps(sig, ensure_ascii=False, indent=2), encoding="utf-8")
    return sig


def verify_skill(d: Path, trusted: dict[str, str]) -> tuple[bool, str]:
    """返回 (是否可信, 说明)"""
    p = d / SIGNATURE_FILE
    if not p.is_file():
        return False, "未签名"
    try:
        sig = json.loads(p.read_text(encoding="utf-8"))
        signer = sig["signer"]
    except (ValueError, KeyError):
        return False, "签名文件损坏"
    if signer not in trusted:
        return False, f"签名者 {signer} 不在信任列表中"
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        return False, '无法验证签名（需要 pip install "No-Agent[sign]"）'
    if sig.get("skill") != d.name:
        return False, "签名对应的技能与目录不一致"
    digests = file_digests(d)
    if digests != sig.get("files"):
        changed = sorted(set(digests.items()) ^ set((sig.get("files") or {}).items()))
        return False, f"签名后文件被修改: {', '.join(sorted({k for k, _ in changed}))}"
    try:
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(trusted[signer]))
        pub.verify(base64.b64decode(sig["signature"]), _payload(d.name, digests))
    except (InvalidSignature, ValueError):
        return False, "签名无效"
    return True, f"已由 {signer} 签名"
