"""Original all-type record normalization for private local export."""

import json
import re
import xml.etree.ElementTree as ET

from .history import HistoryError, TEXT_TYPES, _decode_text


KINDS = {3: "image", 34: "voice", 43: "video", 47: "emoji", 48: "location",
         10000: "system", 244813135921: "quote", 17179869233: "link",
         21474836529: "link", 154618822705: "link", 25769803825: "file",
         34359738417: "file", 103079215153: "file"}
KEY_FIELDS = re.compile(r"(?:[a-z]*key|"
                        r"password|passwd|passphrase|secret|token|authorization|signature|ticket)", re.I)
CREDENTIAL = re.compile(
    r"\b((?:[a-z_]*aeskey|[a-z_]*encryptkey|[a-z_]*authkey|key|password|passwd|"
    r"passphrase|secret|token|authorization|signature|ticket)\s*[=:]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s<>&;,]+)", re.I)


def packed_attachment_hints(value):
    """Extract locator tokens only, never export a packed source blob."""
    if isinstance(value, str):
        value = value.encode("utf-8")
    if not isinstance(value, bytes):
        return []
    hints = []
    pattern = rb"(?<![0-9a-fA-F])([0-9a-fA-F]{32})(?:(?:_[tThHwW])?\.dat)?(?![0-9a-fA-F])"
    for match in re.finditer(pattern, value):
        context = value[max(0, match.start() - 48):match.start()].lower()
        if re.search(rb"(?:key|token|secret|password)\s*[=:>\"']\s*[\"']?$", context):
            continue
        full = match.group(0).decode("ascii")
        hint = {"type": "filename" if full.lower().endswith(".dat") else "hash",
                "value": full.lower()}
        if hint not in hints:
            hints.append(hint)
    return hints


def _field(name):
    return str(name).rsplit("}", 1)[-1].replace("_", "").replace("-", "")


def _clean_structure(value):
    if isinstance(value, dict):
        return {key: _clean_structure(child) for key, child in value.items()
                if not KEY_FIELDS.fullmatch(_field(key))}
    if isinstance(value, list):
        return [_clean_structure(child) for child in value]
    if isinstance(value, str):
        return CREDENTIAL.sub(r"\1[REDACTED]", value)
    return value


def _xml_payload(value):
    hints = []
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b",value,re.I):
        raise HistoryError("消息包含不支持的XML文档类型或实体定义，未展开；原数据库保持。")
    try:
        document = ET.fromstring(value)
    except ET.ParseError:
        return None, hints
    for parent in document.iter():
        for child in list(parent):
            if KEY_FIELDS.fullmatch(_field(child.tag)):
                parent.remove(child)
        for key in list(parent.attrib):
            if KEY_FIELDS.fullmatch(_field(key)):
                del parent.attrib[key]
        for name, content in [*parent.attrib.items(), (parent.tag, parent.text or "")]:
            normalized = _field(name).lower()
            content = str(content).strip()
            hint_type = None
            if normalized in {"filename", "filetitle", "title"} and content:
                hint_type = "filename"
            elif normalized in {"localpath", "filepath", "path", "thumbpath", "attachpath"} and content:
                hint_type = "path"
            elif normalized in {"md5", "hash", "sha256", "filemd5", "cdnthumbmd5"} and re.fullmatch(r"[0-9a-fA-F]{32}|[0-9a-fA-F]{64}", content):
                hint_type = "hash"
            if hint_type:
                hint = {"type": hint_type, "value": content}
                if hint not in hints:
                    hints.append(hint)
        if parent.text:
            parent.text = CREDENTIAL.sub(r"\1[REDACTED]", parent.text)
        for key, content in list(parent.attrib.items()):
            parent.attrib[key] = CREDENTIAL.sub(r"\1[REDACTED]", content)
    return ET.tostring(document, encoding="unicode"), hints


def export_body(kind, content, compression, sender_id):
    message_kind = "text" if kind in TEXT_TYPES else KINDS.get(kind, "unknown")
    result = {"kind": message_kind, "text": "", "raw_payload": "", "attachment_hints": [],
              "media_status": ("pending" if message_kind in {"image", "voice", "video", "file", "emoji"}
                               else "not_applicable"), "body_binary": None}
    try:
        text = _decode_text(content, compression, sender_id)
    except HistoryError:
        # Broken declared compression is an error, not an opaque binary record.
        if compression not in (0, 1, 2, 3) or kind in TEXT_TYPES or not isinstance(content, bytes):
            raise
        # Unknown opaque binary may contain uninspectable secrets. It remains a
        # private source fragment only; publishing/exporting it as base64 would
        # evade the credential-redaction boundary.
        result.update(payload_status="opaque_binary_not_exported",
                      binary_size=len(content), binary_sha256=__import__("hashlib").sha256(content).hexdigest())
        return result
    if kind in TEXT_TYPES:
        result["text"] = text
        result["raw_payload"] = text
        result["payload_status"] = "complete"
        return result
    if not text:
        result["payload_status"] = "empty_nontext_payload"
        return result
    xml, hints = _xml_payload(text)
    if xml is not None:
        result.update(raw_payload=xml, attachment_hints=hints, payload_status="redacted_structured")
        if kind == 49:
            subtype = ET.fromstring(xml).findtext(".//appmsg/type")
            result["kind"] = {"5": "link", "6": "file", "57": "quote"}.get(subtype, "unknown")
            result["media_status"] = "pending" if result["kind"] == "file" else "not_applicable"
    else:
        try:
            parsed = json.loads(text)
            result["raw_payload"] = json.dumps(_clean_structure(parsed), ensure_ascii=False)
            result["payload_status"] = "redacted_structured"
        except (TypeError, ValueError):
            result["raw_payload"] = CREDENTIAL.sub(r"\1[REDACTED]", text)
            result["payload_status"] = "redacted_text"
    return result
