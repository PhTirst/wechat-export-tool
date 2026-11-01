"""Pure AES-128 expanded-key recognition; no filesystem or process access.

Recognizes a complete 176-byte encryption key schedule, and optionally common
equivalent-inverse decryption schedules (round keys reversed, inner keys passed
through InvMixColumns). Both normal byte order and per-32-bit-word byte reversal
are supported. Candidate offsets need not be word aligned.

The NumPy prefilter checks the first AES key-expansion relation in four bytes;
every surviving offset then passes the entire 44-word schedule recurrence.
Callers still have to validate each yielded 16-byte key against their ciphertext.
"""
from __future__ import annotations

SBOX = bytes.fromhex(
    "637c777bf26b6fc53001672bfed7ab76"
    "ca82c97dfa5947f0add4a2af9ca472c0"
    "b7fd9326363ff7cc34a5e5f171d83115"
    "04c723c31896059a071280e2eb27b275"
    "09832c1a1b6e5aa0523bd6b329e32f84"
    "53d100ed20fcb15b6acbbe394a4c58cf"
    "d0efaafb434d338545f9027f503c9fa8"
    "51a3408f929d38f5bcb6da2110fff3d2"
    "cd0c13ec5f974417c4a77e3d645d1973"
    "60814fdc222a908846eeb814de5e0bdb"
    "e0323a0a4906245cc2d3ac629195e479"
    "e7c8376d8dd54ea96c56f4ea657aae08"
    "ba78252e1ca6b4c6e8dd741f4bbd8b8a"
    "703eb5664803f60e613557b986c11d9e"
    "e1f8981169d98e949b1e87e9ce5528df"
    "8ca1890dbfe6426841992d0fb054bb16"
)


def _xtime(value: int) -> int:
    return ((value << 1) ^ (0x11b if value & 0x80 else 0)) & 255


def expand_aes128_key(key: bytes) -> bytes:
    """FIPS 197 AES-128 expansion, with each word in conventional byte order."""
    if len(key) != 16:
        raise ValueError("aes128_key_requires_16_bytes")
    expanded = bytearray(key)
    rcon = 1
    for word in range(4, 44):
        previous = list(expanded[4 * (word - 1):4 * word])
        if word % 4 == 0:
            previous = [SBOX[previous[1]] ^ rcon, SBOX[previous[2]],
                        SBOX[previous[3]], SBOX[previous[0]]]
            rcon = _xtime(rcon)
        expanded.extend(expanded[4 * (word - 4) + byte] ^ previous[byte] for byte in range(4))
    return bytes(expanded)


def _mix_word(word: bytes) -> bytes:
    return bytes(_xtime(word[byte]) ^ _xtime(word[(byte + 1) % 4]) ^
                 word[(byte + 1) % 4] ^ word[(byte + 2) % 4] ^ word[(byte + 3) % 4]
                 for byte in range(4))


def _word_reverse(schedule: bytes) -> bytes:
    return b"".join(schedule[offset:offset + 4][::-1] for offset in range(0, 176, 4))


def _normalize_inverse(schedule: bytes) -> bytes:
    rounds = [schedule[offset:offset + 16] for offset in range(0, 176, 16)]
    encryption = [rounds[-1]]
    for key in rounds[-2:0:-1]:
        encryption.append(b"".join(_mix_word(key[offset:offset + 4]) for offset in range(0, 16, 4)))
    encryption.append(rounds[0])
    return b"".join(encryption)


def scan_aes128_schedules(data: bytes, *, include_word_swapped: bool = True,
                          include_decrypt: bool = True):
    """Yield distinct validated 16-byte keys at arbitrary offsets in ``data``.

    A complete expanded schedule must be present in this input. Chunked callers
    should overlap chunks by at least 175 bytes. NumPy is imported only on use.
    No key schedule or candidate value is logged by this module.
    """
    if len(data) < 176:
        return
    import numpy as np
    view = np.frombuffer(data, dtype=np.uint8)
    sbox = np.frombuffer(SBOX, dtype=np.uint8)
    count = len(data) - 175
    seen = set()

    def xtime_vector(values):
        return (values << np.uint8(1)) ^ ((values >> np.uint8(7)) * np.uint8(0x1b))

    for inverse in ((False, True) if include_decrypt else (False,)):
        for swapped in ((False, True) if include_word_swapped else (False,)):
            def index(word, byte):
                return 4 * word + (3 - byte if swapped else byte)

            # For the inverse representation the original key is last, and the
            # penultimate key is InvMixColumns(E1). Undo MixColumns in the filter.
            first_word, next_word = (40, 36) if inverse else (0, 4)

            def at(offsets, word, byte):
                return view[offsets + index(word, byte)]

            def observed(offsets, byte):
                if not inverse:
                    return at(offsets, next_word, byte)
                a = at(offsets, next_word, byte)
                b = at(offsets, next_word, (byte + 1) % 4)
                return xtime_vector(a) ^ xtime_vector(b) ^ b ^ \
                    at(offsets, next_word, (byte + 2) % 4) ^ at(offsets, next_word, (byte + 3) % 4)

            # Use slices for the first byte to avoid allocating a full int64
            # offset array. Remaining checks touch only ~1/256 of the input.
            def contiguous(word, byte):
                offset = index(word, byte)
                return view[offset:offset + count]

            expected = contiguous(first_word, 0) ^ sbox[contiguous(first_word + 3, 1)] ^ np.uint8(1)
            if inverse:
                a, b = contiguous(next_word, 0), contiguous(next_word, 1)
                first_observed = xtime_vector(a) ^ xtime_vector(b) ^ b ^ \
                    contiguous(next_word, 2) ^ contiguous(next_word, 3)
            else:
                first_observed = contiguous(next_word, 0)
            offsets = np.flatnonzero(expected == first_observed)
            for byte in range(1, 4):
                if not offsets.size:
                    break
                expected = at(offsets, first_word, byte) ^ sbox[at(offsets, first_word + 3, (byte + 1) % 4)]
                offsets = offsets[expected == observed(offsets, byte)]
            for offset in offsets:
                schedule = bytes(data[int(offset):int(offset) + 176])
                if swapped:
                    schedule = _word_reverse(schedule)
                if inverse:
                    schedule = _normalize_inverse(schedule)
                key = schedule[:16]
                if key not in seen and expand_aes128_key(key) == schedule:
                    seen.add(key)
                    yield key
