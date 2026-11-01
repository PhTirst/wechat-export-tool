"""Bounded, read-only local image-key retrieval; never prints key material."""
from __future__ import annotations
import ctypes
from ctypes import wintypes as wt
import hashlib
import json
import math
import os
import re
import struct
import time
from collections import Counter
from pathlib import Path
from Crypto.Cipher import AES
from .security import protect_bytes, unprotect_bytes, private_text_writer, secure_file
from chatlog_keeper import wechat_image as wi

CACHE_FORMAT = 'wechat-export.image-key.dpapi.v1'
SIG = b'\x07\x08V2\x08\x07'
_ASCII_RUN = re.compile(rb'(?<![0-9A-Za-z])[0-9A-Za-z]{16,64}(?![0-9A-Za-z])')
_UTF16_RUN = re.compile(rb'(?<![0-9A-Za-z]\x00)(?:[0-9A-Za-z]\x00){16,64}(?![0-9A-Za-z]\x00)')
_CHUNK_BYTES = 4 * 1024 * 1024
_CHUNK_OVERLAP = 256


def image_key_candidates(data: bytes):
    """Pure candidate extraction, including Windows UTF-16LE strings.

    The image key is AES-128 (16 bytes), independently of database page keys.
    Recognize direct ASCII strings and their 32-hex-digit binary representation.
    A 16-byte key can also be adjacent to an alphanumeric buffer, so inspect
    both edges of bounded 16..64-character runs rather than exact lengths only.
    """
    def candidates(text):
        yield text[:16]
        if len(text) > 16:
            yield text[-16:]
        if len(text) >= 32:
            for encoded in (text[:32], text[-32:]):
                try:
                    yield bytes.fromhex(encoded.decode('ascii'))
                except ValueError:
                    pass
    for pattern, wide in ((_ASCII_RUN, False), (_UTF16_RUN, True)):
        for match in pattern.finditer(data):
            text = match.group()[::2] if wide else match.group()
            yield from candidates(text)
    # AES libraries can discard the original string while retaining round keys.
    # Recover only complete, recurrence-verified AES-128 schedules; ciphertext
    # validation in scan remains mandatory before any candidate is saved.
    from .aes128_schedule import scan_aes128_schedules
    yield from scan_aes128_schedules(data)


def _readable_region(state: int, protection: int) -> bool:
    return (state == 0x1000 and not protection & 0x100 and
            protection & 0xff in (0x02, 0x04, 0x20, 0x40, 0x80))


def _sample_files(files):
    """Prefer recently downloaded distinct images, not lexical hash ordering."""
    dated=[]
    for path in files:
        try:dated.append((path.stat().st_mtime_ns,path))
        except OSError:continue
    newest=[path for _,path in sorted(dated,key=lambda item:item[0],reverse=True)]
    samples, used = [], set()
    for path in newest:
        identity = re.sub(r'_(?:W|t|h)$', '', path.stem, flags=re.I)
        if identity in used:
            continue
        try:
            with path.open('rb') as handle:
                head = handle.read(47)
        except OSError:
            continue
        if (head[:6] == SIG and len(head) >= 31 and
                struct.unpack_from('<I', head, 6)[0] >= 16):
            samples.append((path, head))
            used.add(identity)
        if len(samples) >= 8:
            break
    return samples

def account_fingerprint(account_root:Path)->str:
    """Stable opaque account binding; never embeds the account directory name."""
    normalized=str(Path(account_root).expanduser().resolve()).replace('\\','/').casefold()
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()

def cache_path(cache_root:Path,account_root:Path)->Path:
    """Build an account-separated cache location outside exported artifacts."""
    return Path(cache_root)/account_fingerprint(account_root)/'image-key.dpapi.json'

def target_files(account_root:Path,conversation_id:str):
    """Inspect only this conversation's local attachment tree, without links."""
    if not isinstance(conversation_id,str) or not conversation_id or '\0' in conversation_id or len(conversation_id)>512:
        raise ValueError('invalid_conversation_id')
    account=Path(account_root).absolute()
    root=account/'msg/attach'/hashlib.md5(conversation_id.encode('utf-8')).hexdigest()
    if not root.is_dir():return []
    result=[]
    for path in root.rglob('*.dat'):
        try:
            path.resolve(strict=True).relative_to(root.resolve(strict=True))
            current=path
            while current!=account:
                attributes=current.lstat()
                if current.is_symlink() or getattr(attributes,'st_file_attributes',0)&0x400:
                    raise ValueError('linked_source')
                if current==current.parent:raise ValueError('invalid_source_scope')
                current=current.parent
            if path.is_file():result.append(path)
        except (OSError,ValueError):continue
    return sorted(result)

def _notify(progress_cb,stage,**values):
    if progress_cb is not None:progress_cb({'stage':stage,**values})

def load(cache_file:Path,*,account_root:Path):
    """Read an existing account-bound key cache; never scans or launches apps."""
    if not Path(cache_file).exists():return None,None
    return _load_envelope(cache_file,account_root=account_root)

def _load_envelope(path:Path,*,account_root:Path,allow_unbound=False):
    secure_file(Path(path))
    with Path(path).open('rb') as handle:
        envelope_bytes=handle.read(65537)
    if len(envelope_bytes)>65536:raise ValueError('image_key_envelope_too_large')
    envelope=json.loads(envelope_bytes)
    if not allow_unbound and envelope.get('format')!=CACHE_FORMAT:
        raise ValueError('image_key_cache_format_invalid')
    binding=account_fingerprint(account_root)
    if envelope.get('account_hash') not in ((None,binding) if allow_unbound else (binding,)):
        raise ValueError('image_key_account_mismatch')
    raw=unprotect_bytes(envelope['protected'])
    try:
        obj=json.loads(raw);key=bytes.fromhex(obj['aes']);xor=obj.get('xor')
        if obj.get('account_hash') not in ((None,binding) if allow_unbound else (binding,)):
            raise ValueError('image_key_account_mismatch')
        if len(key)!=16:raise ValueError('image_key_length_invalid')
        if xor is not None and (isinstance(xor,bool) or not isinstance(xor,int) or not 0<=xor<=255):
            raise ValueError('image_xor_key_invalid')
        return key,xor
    finally:raw[:]=b'\x00'*len(raw)

def _valid_image_key(key:bytes,samples)->bool:
    magic=(b'\xff\xd8\xff',b'\x89PNG',b'GIF8',b'RIFF',b'wxgf')
    try:
        cipher=AES.new(key,AES.MODE_ECB)
        for path,head in samples:
            if cipher.decrypt(head[15:31]).startswith(magic):
                # The tail's XOR value does not affect AES PKCS7 validation.
                from Crypto.Util.Padding import unpad
                aes_size=struct.unpack_from('<I',head,6)[0]
                padded_size=aes_size+(16-aes_size%16)
                if padded_size>200*1024*1024:continue
                with path.open('rb') as handle:
                    handle.seek(15);encrypted=handle.read(padded_size)
                if len(encrypted)!=padded_size:continue
                decoded=unpad(cipher.decrypt(encrypted),16)
                if len(decoded)!=aes_size:continue
                if decoded and decoded.startswith(magic):return True
        return False
    except (OSError,ValueError):return False

def _infer_xor(files,key:bytes):
    """Account-scoped votes from exact V2 JPEG/PNG end markers only."""
    votes=Counter();cipher=AES.new(key,AES.MODE_ECB)
    for path in files:
        try:
            with path.open('rb') as handle:
                head=handle.read(31)
                if len(head)<31 or head[:6]!=SIG:continue
                size=path.stat().st_size;xor_size=struct.unpack_from('<I',head,10)[0]
                if xor_size<2 or xor_size>size-15:continue
                prefix=cipher.decrypt(head[15:31])
                if prefix.startswith(b'\xff\xd8\xff'):
                    handle.seek(-2,2);tail=handle.read(2);candidate=tail[0]^255
                    if (tail[1]^217)==candidate:votes[candidate]+=1
                elif prefix.startswith(b'\x89PNG') and xor_size>=8:
                    handle.seek(-8,2);tail=handle.read(8)
                    candidates=[a^b for a,b in zip(tail,b'IEND\xaeB\x60\x82')]
                    if len(set(candidates))==1:votes[candidates[0]]+=1
        except (OSError,ValueError,IndexError):continue
    return votes.most_common(1)[0] if votes else (None,0)

def _save_key(key:bytes,xor,cache_file:Path,*,account_root:Path):
    binding=account_fingerprint(account_root)
    payload=bytearray(json.dumps({'aes':key.hex(),'xor':xor,'account_hash':binding}).encode())
    try:
        protected=protect_bytes(bytes(payload))
    finally:payload[:]=b'\0'*len(payload)
    with private_text_writer(Path(cache_file),secure_parent=True) as handle:
        json.dump({'format':CACHE_FORMAT,'account_hash':binding,'protected':protected},handle)

def accept_candidate(path:Path,*,account_root:Path,conversation_id:str,cache_file:Path,progress_cb=None):
    """Validate a separately DPAPI-protected candidate before canonical storage.

    No key, file path, or message bytes are emitted. This function is deliberately
    separate from scanning and must be called explicitly by the local operator.
    """
    try:
        key,_claimed_xor=_load_envelope(Path(path),account_root=account_root,allow_unbound=True)
        files=target_files(account_root,conversation_id);samples=_sample_files(files)
        if not samples or not _valid_image_key(key,samples):
            _notify(progress_cb,'image_key_candidate_complete',accepted=False,samples=len(samples))
            return None,None
        xor,count=_infer_xor(files,key)
        _save_key(key,xor,cache_file,account_root=account_root)
        _notify(progress_cb,'image_key_candidate_complete',accepted=True,samples=len(samples),
               xor_verified_samples=count,xor_available=xor is not None)
        return key,xor
    except Exception:
        _notify(progress_cb,'image_key_candidate_complete',accepted=False,samples=0)
        return None,None

def _current_user_owns_pid(pid:int)->bool:
    """Compare Windows token SIDs, failing closed if the process is protected."""
    if os.name!='nt':return False
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    advapi=ctypes.WinDLL('advapi32',use_last_error=True)
    kernel.OpenProcess.argtypes=[wt.DWORD,wt.BOOL,wt.DWORD]
    kernel.OpenProcess.restype=wt.HANDLE
    kernel.GetCurrentProcess.argtypes=[];kernel.GetCurrentProcess.restype=wt.HANDLE
    kernel.CloseHandle.argtypes=[wt.HANDLE];kernel.CloseHandle.restype=wt.BOOL
    advapi.OpenProcessToken.argtypes=[wt.HANDLE,wt.DWORD,ctypes.POINTER(wt.HANDLE)]
    advapi.OpenProcessToken.restype=wt.BOOL
    advapi.GetTokenInformation.argtypes=[wt.HANDLE,ctypes.c_int,ctypes.c_void_p,wt.DWORD,ctypes.POINTER(wt.DWORD)]
    advapi.GetTokenInformation.restype=wt.BOOL
    advapi.EqualSid.argtypes=[ctypes.c_void_p,ctypes.c_void_p];advapi.EqualSid.restype=wt.BOOL
    process=kernel.OpenProcess(0x1000,False,pid)
    if not process:return False
    tokens=[]
    try:
        buffers=[]
        for handle in (kernel.GetCurrentProcess(),process):
            token=wt.HANDLE()
            if not advapi.OpenProcessToken(handle,0x8,ctypes.byref(token)):return False
            tokens.append(token)
            needed=wt.DWORD()
            advapi.GetTokenInformation(token,1,None,0,ctypes.byref(needed))
            if not 0<needed.value<=65536:return False
            buffer=ctypes.create_string_buffer(needed.value)
            if not advapi.GetTokenInformation(token,1,buffer,needed,ctypes.byref(needed)):return False
            buffers.append(buffer)
        sids=[ctypes.c_void_p.from_buffer(buffer).value for buffer in buffers]
        return bool(all(sids) and advapi.EqualSid(sids[0],sids[1]))
    finally:
        for token in tokens:kernel.CloseHandle(token)
        kernel.CloseHandle(process)

def _capture_supported()->bool:
    return os.name=='nt' and ctypes.sizeof(ctypes.c_void_p)==8

def scan(*,account_root:Path,conversation_id:str,cache_file:Path,pid:int|None=None,
         timeout:float=90,progress_cb=None):
    """Explicit, read-only capture for current-user WeChat processes only.

    Normal export must call load(), not scan(). This function never starts,
    stops, debugs, or sends input to WeChat. Ciphertext probes are restricted
    to account_root and the single requested conversation.
    """
    if isinstance(timeout,bool) or not isinstance(timeout,(int,float)) or not math.isfinite(timeout) or timeout<=0:
        raise ValueError('timeout_must_be_positive_and_finite')
    files=target_files(account_root,conversation_id);samples=_sample_files(files)
    if not samples:_notify(progress_cb,'image_key_not_needed');return None,None
    def valid(k):
        return _valid_image_key(k,samples)
    key,xor=load(cache_file,account_root=account_root)
    if key and valid(key):
        if xor is None:
            xor,count=_infer_xor(files,key)
            _save_key(key,xor,cache_file,account_root=account_root)
            _notify(progress_cb,'image_key_cached_complete',accepted=True,
                   xor_verified_samples=count,xor_available=xor is not None)
        return key,xor
    if not _capture_supported():
        raise RuntimeError('image_key_capture_requires_64bit_windows')
    if pid is not None and (isinstance(pid,bool) or not isinstance(pid,int) or pid<=0):
        raise ValueError('invalid_process_id')
    selected_pid=pid
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[wt.DWORD,wt.BOOL,wt.DWORD];kernel.OpenProcess.restype=wt.HANDLE
    kernel.ReadProcessMemory.argtypes=[wt.HANDLE,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_size_t,ctypes.POINTER(ctypes.c_size_t)]
    kernel.ReadProcessMemory.restype=wt.BOOL
    class MBI(ctypes.Structure):
        _fields_=[('BaseAddress',ctypes.c_void_p),('AllocationBase',ctypes.c_void_p),('AllocationProtect',wt.DWORD),('alignment',wt.DWORD),('RegionSize',ctypes.c_size_t),('State',wt.DWORD),('Protect',wt.DWORD),('Type',wt.DWORD),('padding',wt.DWORD)]
    kernel.VirtualQueryEx.argtypes=[wt.HANDLE,ctypes.c_void_p,ctypes.POINTER(MBI),ctypes.c_size_t];kernel.VirtualQueryEx.restype=ctypes.c_size_t
    kernel.CloseHandle.argtypes=[wt.HANDLE];kernel.CloseHandle.restype=wt.BOOL
    deadline=time.monotonic()+timeout;seen=set();tested=0;regions=0;key=None
    cursors={};pending_candidates={};rounds=0;last_progress=time.monotonic()
    def candidate_slice(iterator, until):
        nonlocal key,tested
        # Retain the in-memory iterator if a dense chunk outlives this PID's
        # quantum; resuming avoids either dropping candidates or starving PIDs.
        while time.monotonic()<until:
            try:candidate=next(iterator)
            except StopIteration:return None
            fingerprint=hashlib.sha256(candidate).digest()
            if fingerprint in seen:continue
            seen.add(fingerprint);tested+=1
            if valid(candidate):
                key=candidate
                return None
        return iterator
    _notify(progress_cb,'image_key_readonly_scan_started',timeout_seconds=timeout)
    while key is None and time.monotonic()<deadline:
        rounds+=1
        # Re-enumerate throughout the listening window: opening an image can
        # launch a new WeChatAppEx process after the first pass has finished.
        pids=[selected_pid] if selected_pid is not None else wi._get_wechat_image_pids()
        pids=[value for value in pids if _current_user_owns_pid(value)]
        pids=list(dict.fromkeys(pids))
        if pids:
            rotation=(rounds-1)%len(pids)
            pids=pids[rotation:]+pids[:rotation]
        for pid in pids:
            if time.monotonic()>=deadline:break
            handle=kernel.OpenProcess(0x410,False,pid)
            if not handle:continue
            try:
                address=cursors.get(pid,0);mbi=MBI();read_chunks=0
                # A bounded per-PID slice prevents a large main-process heap
                # from exhausting the deadline before preview processes run.
                slice_deadline=min(deadline,time.monotonic()+0.25)
                if pid in pending_candidates:
                    remaining=candidate_slice(pending_candidates.pop(pid),slice_deadline)
                    if remaining is not None:
                        pending_candidates[pid]=remaining
                        continue
                    if key:break
                while address<0x7fffffffffff and time.monotonic()<slice_deadline and read_chunks<2:
                    if not kernel.VirtualQueryEx(handle,address,ctypes.byref(mbi),ctypes.sizeof(mbi)):
                        address=0;break
                    base=mbi.BaseAddress or 0;size=mbi.RegionSize;end=base+size
                    if size<=0 or end<=address:
                        address=0;break
                    if _readable_region(mbi.State,mbi.Protect):
                        regions+=1;read_chunks+=1
                        start=max(address,base);step=min(_CHUNK_BYTES,end-start)
                        amount=min(step+_CHUNK_OVERLAP,end-start)
                        buffer=ctypes.create_string_buffer(amount);n=ctypes.c_size_t()
                        kernel.ReadProcessMemory(handle,start,buffer,amount,ctypes.byref(n))
                        address=start+step
                        remaining=candidate_slice(image_key_candidates(buffer.raw[:n.value]),slice_deadline)
                        if remaining is not None:
                            pending_candidates[pid]=remaining
                            break
                    else:
                        address=end
                    if key:break
                cursors[pid]=address if address<0x7fffffffffff else 0
            finally:kernel.CloseHandle(handle)
            if key:break
        now=time.monotonic()
        if key is None and now-last_progress>=10:
            _notify(progress_cb,'image_key_scan_progress',candidates=tested,regions=regions,
                   rounds=rounds,processes=len(pids))
            last_progress=now
        if key is None and now<deadline:
            time.sleep(min(0.25,deadline-now))
    if key is None:
        _notify(progress_cb,'image_key_scan_complete',found=False,candidates=tested,regions=regions);return None,None
    xor,count=_infer_xor(files,key)
    _save_key(key,xor,cache_file,account_root=account_root)
    _notify(progress_cb,'image_key_scan_complete',found=True,candidates=tested,regions=regions,xor_verified_samples=count)
    return key,xor
