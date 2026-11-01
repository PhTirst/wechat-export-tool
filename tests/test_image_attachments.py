"""Synthetic-only coverage for the scoped image/sticker exporter."""
import base64
import hashlib
import io
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from wechat_export_tool import image_attachments as images
from PIL import Image
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad


def picture(color=(31, 90, 160), fmt="PNG"):
    output = io.BytesIO()
    Image.new("RGB", (16, 16), color).save(output, fmt)
    return output.getvalue()


def v2(payload, key=b"0123456789abcdef", xor=0x61):
    aes_size, xor_size = 16, 16
    return (images.V2 + struct.pack("<II", aes_size, xor_size) + b"\0" +
            AES.new(key, AES.MODE_ECB).encrypt(pad(payload[:aes_size], 16)) +
            payload[aes_size:-xor_size] + bytes(value ^ xor for value in payload[-xor_size:]))


class ImageAttachmentsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic_image_export_")
        self.root = Path(self.temp.name)
        self.account = self.root / "synthetic_account"
        self.account.mkdir()
        self.out = self.root / "export"
        self.chat = "synthetic_contact"
        self.folder = self.account / "msg" / "attach" / hashlib.md5(self.chat.encode()).hexdigest() / "2026-10" / "Img"
        self.folder.mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def collect(self, messages, rows, output, image_key=None, xor_key=None, **kwargs):
        return images.collect_images(messages, rows, output, account_root=self.account,
                                     image_key=image_key, xor_key=xor_key, **kwargs)

    def message(self, server="11", kind=3, meta=None):
        return {"conversation_id": self.chat, "msg_type": kind, "server_id": server,
                "ts_iso": "2026-10-19T12:30:00+08:00", "attachment_meta": meta or {}}

    def row(self, server="11", kind=3, packed=b"", xml=""):
        return {"row": {"server_id": server, "local_type": kind,
                        "packed_info_data": {"$bytes_base64": base64.b64encode(packed).decode()},
                        "message_content": xml}}

    def test_legacy_uses_packed_id_not_xml_md5(self):
        digest = "a" * 32
        image = picture()
        (self.folder / (digest + "_W.dat")).write_bytes(bytes(value ^ 0x57 for value in image))
        inner = b"\x22\x20" + digest.encode()
        raw = self.row(packed=b"\x1a" + bytes([len(inner)]) + inner,
                       xml='<msg><img md5="' + "b" * 32 + '"/></msg>')
        result = self.collect([self.message()], [raw], self.out)["11"][0]
        self.assertEqual(result["status"], "available")
        self.assertEqual((self.out / result["path"]).read_bytes(), image)

    def test_v2_explicit_key_and_thumbnail_fallback(self):
        digest = "c" * 32
        payload = picture(fmt="JPEG")
        (self.folder / (digest + "_W.dat")).write_bytes(b"broken")
        (self.folder / (digest + "_t.dat")).write_bytes(v2(payload))
        result = self.collect([self.message(meta={"file_md5": digest})], [], self.out,
                                       b"0123456789abcdef", 0x61)["11"][0]
        self.assertEqual(result["status"], "available")
        self.assertTrue(result["thumbnail"])
        self.assertIn("缩略图", result["reason"])

    def test_v2_missing_or_wrong_key_is_not_available(self):
        digest = "d" * 32
        (self.folder / (digest + ".dat")).write_bytes(v2(picture()))
        message = self.message(meta={"file_md5": digest})
        result = self.collect([message], [], self.out)["11"][0]
        self.assertEqual(result["reason"], "image_key_unavailable")
        result = self.collect([message], [], self.out, b"wrongwrongwrong!", 0x61)["11"][0]
        self.assertEqual(result["status"], "missing")
        self.assertEqual(result["reason"], "image_key_or_payload_invalid")

    def test_xml_key_only_accepted_after_full_validation(self):
        digest = "e" * 32
        key = b"xml-key-sample!!"
        # This is a synthetic CDN/local key coincidence; no assumption is made
        # that a real XML CDN key can decrypt a local DAT.
        self.assertEqual(len(key), 16)
        (self.folder / (digest + ".dat")).write_bytes(v2(picture(), key))
        raw = self.row(xml=f'<msg><img aeskey="{key.hex()}"/></msg>')
        result = self.collect([self.message(meta={"file_md5": digest})], [raw], self.out,
                                       xor_key=0x61)["11"][0]
        self.assertEqual(result["status"], "available")

    def test_exact_local_emoji_and_missing_sticker(self):
        digest = "f" * 32
        path = self.account / "cache" / "2026-10" / "Emoticon" / digest[:2] / digest
        path.parent.mkdir(parents=True)
        payload = picture(fmt="GIF")
        path.write_bytes(payload)
        messages = [self.message(kind=47), self.message(server="12", kind=47)]
        rows = [self.row(kind=47, xml=f'<msg><emoji md5="{digest}"/></msg>'),
                self.row(server="12", kind=47, xml='<msg><emoji md5="' + "0" * 32 + '"/></msg>')]
        result = self.collect(messages, rows, self.out)
        self.assertEqual(result["11"][0]["mime"], "image/gif")
        self.assertEqual((self.out / result["11"][0]["path"]).read_bytes(), payload)
        self.assertEqual(result["12"][0]["status"], "missing")

    def test_month_and_conversation_scope(self):
        digest = "1" * 32
        other = self.account / "msg" / "attach" / ("0" * 32) / "2026-10" / "Img"
        other.mkdir(parents=True)
        (other / (digest + ".dat")).write_bytes(picture())
        result = self.collect([self.message(meta={"file_md5": digest})], [], self.out)
        self.assertEqual(result["11"][0]["status"], "missing")
        with self.assertRaises(ValueError):
            self.collect([self.message(), {**self.message(), "conversation_id": "other"}], [], self.out)

    def test_wxgf_static_preview_and_original_are_preserved(self):
        stream = picture((255, 255, 255), "JPEG") + picture((20, 30, 40), "JPEG")
        fake_ffmpeg = self.root / "fake_ffmpeg.exe"
        fake_ffmpeg.write_bytes(b"synthetic")
        images.secure_directory(self.out)
        original = b"wxgf" + b"\x00\x00\x00\x01" + b"synthetic_not_real_hevc"
        with patch.object(images.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=stream)):
            item = images._materialize(original, self.out, self.out, ffmpeg=fake_ffmpeg)
        self.assertEqual(item["status"], "available")
        self.assertEqual((self.out / item["original_path"]).read_bytes(), original)
        self.assertIn("静态预览", item["reason"])

    def test_parallel_jobs_are_deduplicated_and_progress_is_main_thread(self):
        payload = picture()
        messages = []
        for number in range(12):
            digest = f"{number % 3:032x}"
            (self.folder / (digest + ".dat")).write_bytes(payload)
            messages.append(self.message(server=str(100 + number), meta={"file_md5": digest}))
        caller = threading.get_ident()
        progress = []
        def callback(value):
            self.assertEqual(threading.get_ident(), caller)
            progress.append(value)
        with patch.object(images, "_materialize", wraps=images._materialize) as materialize:
            result = self.collect(messages, [], self.out, workers=6, progress_cb=callback)
        self.assertEqual(materialize.call_count, 3)
        self.assertEqual(len(result), 12)
        self.assertTrue(all(items[0]["status"] == "available" for items in result.values()))
        self.assertEqual(len(list((self.out / "附件" / "图片").glob("*.png"))), 1)
        self.assertEqual(progress[-1]["completed"], 12)
        self.assertEqual(progress[-1]["available"], 12)


if __name__ == "__main__":
    unittest.main()
