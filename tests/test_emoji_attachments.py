"""Synthetic in-memory database/package/AES tests, without account/key access."""
import hashlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from PIL import Image
from wechat_export_tool import emoji_attachments as emoji
from wechat_export_tool import image_attachments as images


def picture():
    stream = io.BytesIO()
    Image.new('RGB', (16, 16), (41, 67, 130)).save(stream, 'GIF')
    return stream.getvalue()


class EmojiTests(unittest.TestCase):
    def test_blob_ascii_hex_keys_keep_hex_and_raw_interpretations(self):
        for size in (16, 24, 32):
            key = bytes(range(size))
            raw_hex = key.hex().encode('ascii')
            candidates = emoji.aes_keys(raw_hex)
            self.assertIn(key, candidates)
            if len(raw_hex) in (16, 24, 32):
                self.assertIn(raw_hex, candidates)
            payload = picture()
            cipher = AES.new(key, AES.MODE_ECB).encrypt(pad(payload, 16))
            self.assertEqual(emoji.decode_emoji_aes(cipher, [raw_hex], images._image_format), payload)
        self.assertEqual(emoji.aes_keys(b'not-hex' * 9), [])

    def test_tagless_gcm_requires_exact_checksum_and_complete_image(self):
        payload, key, nonce = picture(), b'synthetic-key!!!', b'012345678901'
        digest = hashlib.md5(payload).hexdigest()
        encrypted = AES.new(key, AES.MODE_GCM, nonce=nonce).encrypt(payload)
        for raw in (nonce + encrypted, encrypted + nonce):
            self.assertEqual(emoji.decode_emoji_aes(raw, [key], images._image_format,
                                                   expected_md5s=[digest]), payload)
            self.assertIsNone(emoji.decode_emoji_aes(raw, [key], images._image_format))
            self.assertIsNone(emoji.decode_emoji_aes(raw, [key], images._image_format,
                                                    expected_md5s=['f' * 32]))
            self.assertIsNone(emoji.decode_emoji_aes(raw, [b'wrongwrongwrong!'], images._image_format,
                                                    expected_md5s=[digest]))
        # Even a shallow third-party validation callback cannot permit a
        # hash-matching non-image when GCM authentication is unavailable.
        nonimage = b'not a picture at all!'
        raw = nonce + AES.new(key, AES.MODE_GCM, nonce=nonce).encrypt(nonimage)
        self.assertIsNone(emoji.decode_emoji_aes(raw, [key], lambda _: True,
            expected_md5s=[hashlib.md5(nonimage).hexdigest()]))

    def test_authenticated_gcm_nonce_tag_layouts(self):
        payload, key, nonce = picture(), b'synthetic-key!!!', b'012345678901'
        cipher, tag = AES.new(key, AES.MODE_GCM, nonce=nonce).encrypt_and_digest(payload)
        for raw in (nonce + cipher + tag, nonce + tag + cipher,
                    cipher + tag + nonce, tag + cipher + nonce,
                    cipher + nonce + tag, tag + nonce + cipher):
            self.assertEqual(emoji.decode_emoji_aes(raw, [key], images._image_format), payload)

    def test_short_tail_block_alignment_requires_checksum(self):
        payload, key = picture(), b'synthetic-key!!!'
        digest, iv = hashlib.md5(payload).hexdigest(), b'explicit-test-iv'
        encrypted = [AES.new(key, AES.MODE_ECB).encrypt(pad(payload, 16)),
                     AES.new(key, AES.MODE_CBC, iv=b'\0' * 16).encrypt(pad(payload, 16)),
                     iv + AES.new(key, AES.MODE_CBC, iv=iv).encrypt(pad(payload, 16))]
        for cipher in encrypted:
            raw = cipher + b'trailer'
            self.assertEqual(emoji.decode_emoji_aes(raw, [key], images._image_format,
                                                   expected_md5s=[digest]), payload)
            self.assertIsNone(emoji.decode_emoji_aes(raw, [key], images._image_format))
            self.assertIsNone(emoji.decode_emoji_aes(raw, [key], images._image_format,
                                                    expected_md5s=['a' * 32]))
            self.assertIsNone(emoji.decode_emoji_aes(raw, [b'wrongwrongwrong!'], images._image_format,
                                                    expected_md5s=[digest]))

    def test_tagless_callsite_uses_only_explicit_database_hash_alias(self):
        payload, key, nonce = picture(), b'synthetic-key!!!', b'012345678901'
        target, alias = 'a' * 32, hashlib.md5(payload).hexdigest()
        metadata = {target: {'aes_keys': [key.hex().encode('ascii')],
                             'extern_md5s': [alias], 'packages': []}}
        with tempfile.TemporaryDirectory(prefix='synthetic-emoji-gcm-') as temp:
            root = Path(temp)
            account, output = root / 'account', root / 'export'
            path = account / 'business/emoticon/Persist' / target[:2] / target
            path.parent.mkdir(parents=True)
            path.write_bytes(nonce + AES.new(key, AES.MODE_GCM, nonce=nonce).encrypt(payload))
            messages = [{'conversation_id':'synthetic','msg_type':47,'server_id':1}]
            rows = [{'row': {'local_type':47,'server_id':1,
                'message_content':f'<msg><emoji md5="{target}"/></msg>'}}]
            item = images.collect_images(messages, rows, output, account_root=account, emoji_metadata=metadata)['1'][0]
            self.assertEqual(item['status'], 'available')
            self.assertEqual((output/item['path']).read_bytes(), payload)
            metadata[target]['extern_md5s'] = []
            self.assertEqual(images.collect_images(messages, rows, output, account_root=account,
                emoji_metadata=metadata)['1'][0]['status'], 'missing')

    def test_complete_animated_image_validation(self):
        stream = io.BytesIO()
        frames = [Image.new('RGB', (40, 40), color) for color in ('red', 'green', 'blue')]
        frames[0].save(stream, 'GIF', save_all=True, append_images=frames[1:], duration=20, loop=0)
        full = stream.getvalue()
        self.assertTrue(images._image_format(full))
        self.assertIsNone(images._image_format(full[:-30]))

    def test_per_emoji_aes_ecb_cbc_and_authenticated_gcm(self):
        payload, key = picture(), b'synthetic-key!!!'
        for raw in (AES.new(key, AES.MODE_ECB).encrypt(pad(payload, 16)),
                    AES.new(key, AES.MODE_CBC, iv=b'\0' * 16).encrypt(pad(payload, 16))):
            self.assertEqual(emoji.decode_emoji_aes(raw, [key], images._image_format), payload)
            self.assertIsNone(emoji.decode_emoji_aes(raw, [b'wrongwrongwrong!'], images._image_format))
        nonce = b'012345678901'
        encrypted, tag = AES.new(key, AES.MODE_GCM, nonce=nonce).encrypt_and_digest(payload)
        self.assertEqual(emoji.decode_emoji_aes(nonce + encrypted + tag, [key], images._image_format), payload)
        self.assertIsNone(emoji.decode_emoji_aes(nonce + encrypted + b'0' * 16, [key], images._image_format))

    def test_exact_metadata_store_bounds_and_explicit_alias(self):
        conn = sqlite3.connect(':memory:')
        conn.executescript('CREATE TABLE kNonStoreEmoticonTable(md5 TEXT,aes_key TEXT,extern_md5 TEXT);'
                          'CREATE TABLE kStoreEmoticonFilesTable(md5_ TEXT,package_id_ TEXT,emoticon_size_ INT,emoticon_offset_ INT,thumb_size_ INT,thumb_offset_ INT);')
        digest, other, alias = 'a' * 32, 'b' * 32, 'c' * 32
        key = b'synthetic-key!!!'
        payload = picture()
        conn.executemany('INSERT INTO kNonStoreEmoticonTable VALUES(?,?,?)', [(digest,key.hex(),alias),(other,key.hex(),'d'*32)])
        conn.executemany('INSERT INTO kStoreEmoticonFilesTable VALUES(?,?,?,?,?,?)', [(digest,'synthetic-package',len(payload),9,0,0), (other,'wrong-package',9,0,0,0)])
        meta = emoji.load_emoji_metadata(conn, {digest})
        self.assertEqual(set(meta), {digest})
        self.assertEqual(meta[digest]['extern_md5s'], [alias])
        self.assertEqual(meta[digest]['aes_keys'][0], key)
        package = meta[digest]['packages'][0]
        self.assertEqual(package['offset'], 9)
        with tempfile.TemporaryDirectory(prefix='synthetic-emoji-') as temp:
            root = Path(temp)
            account, output = root / 'account', root / 'export'
            path = account / package['path']
            path.parent.mkdir(parents=True)
            path.write_bytes(b'container' + payload + b'trailer')
            messages = [{'conversation_id':'synthetic','msg_type':47,'server_id':1}]
            raw = [{'row':{'local_type':47,'server_id':1,'message_content':f'<msg><emoji md5="{digest}"/></msg>'}}]
            self.assertEqual(images.selected_emoji_md5s(messages, raw), {digest})
            item = images.collect_images(messages, raw, output, account_root=account, emoji_metadata=meta)['1'][0]
            self.assertEqual(item['status'], 'available')
            self.assertEqual((output / item['path']).read_bytes(), payload)
            package['offset'] = path.stat().st_size
            self.assertEqual(images.collect_images(messages, raw, output, account_root=account, emoji_metadata=meta)['1'][0]['status'], 'missing')
            # The selected digest can use only the DB's explicit alias.
            alias_path = account / 'business/emoticon/Persist' / alias[:2] / alias
            alias_path.parent.mkdir(parents=True)
            alias_path.write_bytes(AES.new(key,AES.MODE_ECB).encrypt(pad(payload,16)))
            item = images.collect_images(messages, raw, output, account_root=account, emoji_metadata=meta)['1'][0]
            self.assertEqual(item['status'], 'available')
            self.assertEqual((output / item['path']).read_bytes(), payload)
        conn.close()

    def test_xml_key_without_database(self):
        digest, key, payload = 'e' * 32, b'synthetic-key!!!', picture()
        with tempfile.TemporaryDirectory(prefix='synthetic-emoji-xml-') as temp:
            root = Path(temp)
            account, output = root / 'account', root / 'export'
            path = account / 'business/emoticon/Persist' / digest[:2] / digest
            path.parent.mkdir(parents=True)
            path.write_bytes(AES.new(key,AES.MODE_ECB).encrypt(pad(payload,16)))
            messages = [{'conversation_id':'synthetic','msg_type':47,'server_id':1}]
            raw = [{'row':{'local_type':47,'server_id':1,'message_content':f'<msg><emoji md5="{digest}" aeskey="{key.hex()}"/></msg>'}}]
            item = images.collect_images(messages,raw,output,account_root=account)['1'][0]
            self.assertEqual(item['status'],'available')
            self.assertEqual((output/item['path']).read_bytes(),payload)


if __name__ == '__main__':
    unittest.main()
