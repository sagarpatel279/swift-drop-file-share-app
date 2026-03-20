"""
peer.py — SwiftDrop v6  |  Integrity-First LAN Transfer
=========================================================
Fixes over v5:

UPLOAD ENGINE (mobile):
  BUG 1: adaptive _cs changed offsets mid-transfer → corruption.
    Fix: server issues immutable upload plan (/api/upload-plan).
         chunk_size NEVER changes. offset = cidx × plan_cs always.
  BUG 2: rec.chunk_size set from len(arrived_data) if last chunk first → wrong.
    Fix: chunk_size from plan only.
  BUG 3: new tid on every retry → resume never worked → 65-min transfers.
    Fix: tid stored in sessionStorage/localStorage keyed by (fname+fsize).
         Same file always gets same tid. Resume picks up from sidecar.
  BUG 4: PAR=4 on iOS → network congestion → 90s stalls per batch.
    Fix: plan returns par=2 for iOS, par=3 for Android.
  BUG 5: no per-chunk integrity check.
    Fix: browser computes CRC32, sends X-CRC32. Server rejects bad chunks → 409.
  BUG 6: stat().st_size == fsize after truncate → false completion.
    Fix: _bytes_written counter. Completion: _bytes_written == file_size.

VIDEO PLAYER:
  - FFmpeg transcoding for HEVC/MKV/AVI files browsers can't decode natively
  - /api/probe/<fid> returns codec + audio stream list
  - Multi-audio-track switching in player
  - Skip ±10s buttons
  - Transcoding badge + warning if FFmpeg missing
  - Keyboard shortcuts: Space, ←/→ (seek), ↑/↓ (volume), M, F, C

FIXES carried from v5:
  - Stable filename-based file IDs (no path-UUID restarts)
  - Sidecar (.chunks.json) for crash-resume
  - _respond() swallows ConnectionResetError silently
  - Reconcile engine: after WiFi drop, ask server which chunks it has
  - Delete button on every file row
"""

import os, sys, json, time, uuid, socket, hashlib, logging, zlib, mimetypes
import struct, threading, webbrowser, base64, tempfile, shutil, subprocess
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, parse_qs, unquote
from typing import Dict, List, Optional
from enum import Enum

# ══════════════════════════════════════════════════════════════
# BASE DIR
# ══════════════════════════════════════════════════════════════
def _get_base_dir():
    candidates = []
    if sys.platform == "win32":
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(32768)
            n = ctypes.windll.kernel32.GetModuleFileNameW(0, buf, 32768)
            if n > 0: candidates.append(Path(buf.value))
        except Exception: pass
    candidates.append(Path(sys.executable))
    if sys.argv: candidates.append(Path(sys.argv[0]))
    try: candidates.append(Path(__file__))
    except Exception: pass
    tmp = Path(tempfile.gettempdir()).resolve()
    for p in candidates:
        try:
            r = p.resolve()
            if tmp not in r.parents and r.exists(): return r.parent
        except Exception: pass
    return Path.cwd()

BASE_DIR   = _get_base_dir()
OUTPUT_DIR = BASE_DIR / "received"
TMP_DIR    = BASE_DIR / "_tmp"

# ══════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════
TRANSFER_PORT   = 9000
UI_PORT         = 8080
DISCOVERY_PORT  = 9001
CHUNK_SIZE      = 8 * 1024 * 1024
THREADS         = 8
MAX_RETRIES     = 3
RETRY_DELAY     = 1.5
TCP_BUF         = 4 * 1024 * 1024
STREAM_BLOCK    = 512 * 1024
TRANSCODE_BLOCK = 188 * 1024        # MPEG-TS aligned
MOB_SMALL       = 2 * 1024 * 1024   # < 200 MB files
MOB_MEDIUM      = 4 * 1024 * 1024   # 200 MB – 1 GB
MOB_LARGE       = 8 * 1024 * 1024   # > 1 GB

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("swiftdrop")
_shutdown = threading.Event()

# ══════════════════════════════════════════════════════════════
# FFMPEG  (optional — enables transcoding for unsupported codecs)
# ══════════════════════════════════════════════════════════════
FFMPEG: Optional[str] = None   # set in main() after BASE_DIR is known

def _find_ffmpeg() -> Optional[str]:
    """Search for ffmpeg: next to exe, PATH, common Windows paths."""
    for name in ("ffmpeg.exe", "ffmpeg"):
        for d in (BASE_DIR, BASE_DIR / "ffmpeg"):
            p = d / name
            if p.exists(): return str(p)
    found = shutil.which("ffmpeg")
    if found: return found
    if sys.platform == "win32":
        for d in (r"C:\ffmpeg\bin", r"C:\Program Files\ffmpeg\bin",
                  r"C:\Program Files (x86)\ffmpeg\bin"):
            p = Path(d) / "ffmpeg.exe"
            if p.exists(): return str(p)
    return None

ALWAYS_TRANSCODE = {".mkv", ".avi", ".flv", ".wmv", ".ts", ".m2ts"}

def _ffprobe(fpath: Path) -> dict:
    """Return {video_codec, audio_streams:[{index,codec,label}]} or {}."""
    if not FFMPEG: return {}
    ffprobe_name = "ffprobe.exe" if sys.platform == "win32" else "ffprobe"
    fp = Path(FFMPEG).with_name(ffprobe_name)
    if not fp.exists():
        fp2 = shutil.which("ffprobe")
        if not fp2: return {}
        fp = Path(fp2)
    try:
        r = subprocess.run(
            [str(fp), "-v", "quiet", "-print_format", "json", "-show_streams", str(fpath)],
            capture_output=True, timeout=12)
        data = json.loads(r.stdout)
        vc = ""; audio = []
        for s in data.get("streams", []):
            t = s.get("codec_type", "")
            if t == "video" and not vc:
                vc = s.get("codec_name", "").lower()
            elif t == "audio":
                tags  = s.get("tags", {})
                label = tags.get("title", "") or tags.get("language", "") or f"Track {len(audio)+1}"
                audio.append({"index": s.get("index", 0),
                               "codec": s.get("codec_name", ""),
                               "label": label})
        return {"video_codec": vc, "audio_streams": audio}
    except Exception: return {}

def _needs_transcode(fpath: Path) -> bool:
    ext = fpath.suffix.lower()
    if ext in ALWAYS_TRANSCODE: return True
    if ext in (".mp4", ".m4v", ".mov"):
        vc = _ffprobe(fpath).get("video_codec", "")
        if vc and vc not in ("h264", "avc", "avc1", "vp8", "vp9", "av1", "theora"):
            return True
    return False

# ══════════════════════════════════════════════════════════════
# STATE MACHINE
# ══════════════════════════════════════════════════════════════
class TState(str, Enum):
    QUEUED="queued"; IN_PROGRESS="in_progress"; PAUSED="paused"
    CANCELLED="cancelled"; DONE="done"; FAILED="failed"

_TRANSITIONS = {
    TState.QUEUED:      {TState.IN_PROGRESS, TState.CANCELLED},
    TState.IN_PROGRESS: {TState.PAUSED, TState.CANCELLED, TState.DONE, TState.FAILED},
    TState.PAUSED:      {TState.IN_PROGRESS, TState.CANCELLED},
    TState.CANCELLED: set(), TState.DONE: set(), TState.FAILED: set(),
}

# ══════════════════════════════════════════════════════════════
# BINARY PROTOCOL  (PC↔PC)
# ══════════════════════════════════════════════════════════════
MAGIC=b"XFER"; PROTO_VER=6
MSG_INIT=0x01; MSG_CHUNK=0x02; MSG_FINALIZE=0x03
MSG_ACK=0x10; MSG_ERROR=0x11; MSG_CANCEL=0x12; MSG_PAUSE=0x13; MSG_RESUME=0x14
HDR_FMT="!4sBBHI"; HDR_SIZE=struct.calcsize(HDR_FMT)

def _send_msg(sock, mtype, payload: bytes):
    sock.sendall(struct.pack(HDR_FMT, MAGIC, PROTO_VER, mtype, 0, len(payload)) + payload)

def _recv_exact(sock, n):
    buf=bytearray(n); mv=memoryview(buf); pos=0
    while pos < n:
        got = sock.recv_into(mv[pos:], n-pos)
        if not got: raise ConnectionError("closed")
        pos += got
    return bytes(buf)

def _recv_msg(sock):
    raw = _recv_exact(sock, HDR_SIZE)
    magic, _, mtype, _, plen = struct.unpack(HDR_FMT, raw)
    if magic != MAGIC: raise ValueError(f"bad magic {magic!r}")
    return mtype, (_recv_exact(sock, plen) if plen else b"")

def _crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF

def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(8*1024*1024), b""): h.update(blk)
    return h.hexdigest()

def _tcp_sock(host, port, timeout=60):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    for opt in (socket.SO_SNDBUF, socket.SO_RCVBUF):
        try: s.setsockopt(socket.SOL_SOCKET, opt, TCP_BUF)
        except Exception: pass
    s.connect((host, port))
    return s

# ══════════════════════════════════════════════════════════════
# TRANSFER RECORD
# _bytes_written tracks actual bytes written — NOT fooled by truncate().
# chunk_size is IMMUTABLE after plan creation.
# ══════════════════════════════════════════════════════════════
class XferError(Exception): pass

class TransferRecord:
    def __init__(self, tid, filename, file_size, total_chunks,
                 chunk_size, output_path, direction, source="pc"):
        self.transfer_id  = tid
        self.filename     = filename
        self.file_size    = file_size
        self.total_chunks = total_chunks
        self.chunk_size   = chunk_size   # IMMUTABLE
        self.output_path  = output_path
        self.direction    = direction
        self.source       = source
        self.peer_ip      = ""
        self._received:      set = set()
        self._bytes_written: int = 0
        self._lock = threading.Lock()
        self._state = TState.IN_PROGRESS
        self._slk   = threading.Lock()
        self.pause_ev  = threading.Event(); self.pause_ev.set()
        self.cancel_ev = threading.Event()
        self.start_time = time.monotonic()
        self._paused_at = None
        self._paused_s  = 0.0

    def mark(self, idx: int, nbytes: int):
        with self._lock:
            if idx not in self._received:
                self._received.add(idx)
                self._bytes_written += nbytes

    def received_count(self):
        with self._lock: return len(self._received)

    def received_list(self):
        with self._lock: return list(self._received)

    def missing(self):
        with self._lock: return [i for i in range(self.total_chunks) if i not in self._received]

    @property
    def complete(self):
        with self._lock: return len(self._received) == self.total_chunks

    def bytes_ok(self):
        with self._lock: return self._bytes_written == self.file_size

    @property
    def state(self):
        with self._slk: return self._state

    def transition(self, new):
        with self._slk:
            if new not in _TRANSITIONS.get(self._state, set()): return False
            self._state = new; return True

    def pause(self):
        if not self.transition(TState.PAUSED): return False
        self.pause_ev.clear(); self._paused_at = time.monotonic()
        log.info(f"[{self.filename}] PAUSED"); return True

    def resume(self):
        if not self.transition(TState.IN_PROGRESS): return False
        if self._paused_at:
            self._paused_s += time.monotonic() - self._paused_at; self._paused_at = None
        self.pause_ev.set(); log.info(f"[{self.filename}] RESUMED"); return True

    def cancel(self):
        with self._slk:
            if self._state in (TState.DONE, TState.FAILED, TState.CANCELLED): return False
            self._state = TState.CANCELLED
        self.cancel_ev.set(); self.pause_ev.set(); log.info(f"[{self.filename}] CANCELLED"); return True

    def check(self):
        self.pause_ev.wait()
        if self.cancel_ev.is_set(): raise XferError(f"{self.transfer_id} cancelled")

    def elapsed(self):
        total  = time.monotonic() - self.start_time
        paused = self._paused_s + (time.monotonic() - self._paused_at if self._paused_at else 0)
        return max(0.001, total - paused)

    def to_dict(self):
        with self._lock: n = len(self._received); bw = self._bytes_written
        el = self.elapsed(); spd = bw/el/1e6
        pct = bw/self.file_size*100 if self.file_size else 0
        return {"transfer_id":self.transfer_id,"filename":self.filename,
                "file_size":self.file_size,"pct":round(min(pct,100),1),
                "speed":round(spd,1),"status":self.state.value,
                "direction":self.direction,"source":self.source,
                "peer_ip":self.peer_ip,"chunks_done":n,"chunks_total":self.total_chunks}

# ══════════════════════════════════════════════════════════════
# TRANSFER MANAGER
# ══════════════════════════════════════════════════════════════
class TransferManager:
    def __init__(self):
        self._recs: Dict[str, TransferRecord] = {}
        self._lk = threading.Lock()
        self._clks: Dict[str, threading.Lock] = {}
        self._clk = threading.Lock()
        threading.Thread(target=self._reaper, daemon=True).start()

    def creation_lock(self, tid):
        with self._clk:
            if tid not in self._clks: self._clks[tid] = threading.Lock()
            return self._clks[tid]

    def add(self, r):
        with self._lk: self._recs[r.transfer_id] = r

    def get(self, tid) -> Optional[TransferRecord]:
        with self._lk: return self._recs.get(tid)

    def remove(self, tid):
        with self._lk: self._recs.pop(tid, None)
        with self._clk: self._clks.pop(tid, None)

    def all_progress(self):
        with self._lk: return [r.to_dict() for r in self._recs.values()]

    def pause(self, tid):
        r = self.get(tid); return r.pause() if r else False

    def resume(self, tid):
        r = self.get(tid); return r.resume() if r else False

    def cancel(self, tid):
        r = self.get(tid)
        if not r: return False
        ok = r.cancel()
        if ok: threading.Thread(target=self._cleanup, args=(r,), daemon=True).start()
        return ok

    def _cleanup(self, r):
        time.sleep(1.5)
        try:
            if r.output_path.exists() and r.output_path.suffix == ".tmp":
                r.output_path.unlink(); log.info(f"Cleaned: {r.output_path.name}")
        except Exception as e: log.warning(f"Cleanup: {e}")

    def _reaper(self):
        while not _shutdown.is_set():
            time.sleep(30); cutoff = time.monotonic() - 90
            with self._lk:
                dead = [t for t,r in self._recs.items()
                        if r.state in (TState.DONE,TState.CANCELLED,TState.FAILED)
                        and r.start_time < cutoff]
                for t in dead: del self._recs[t]

TM = TransferManager()

# ══════════════════════════════════════════════════════════════
# DISK HELPERS
# ══════════════════════════════════════════════════════════════
def _preallocate(path: Path, size: int):
    try:
        with open(path, "wb") as fh:
            if hasattr(os, "posix_fallocate"): os.posix_fallocate(fh.fileno(), 0, size)
            else: fh.truncate(size)
        log.info(f"Pre-alloc {path.name} ({size:,}B)")
    except Exception as e:
        log.warning(f"Pre-alloc skip: {e}"); path.touch(exist_ok=True)

def _write_chunk_atomic(path: Path, offset: int, data: bytes):
    with open(path, "r+b") as fh: fh.seek(offset); fh.write(data); fh.flush()

def _tmp_path(fname: str) -> Path: return TMP_DIR / (fname + ".tmp")

def _safe_dest(dest: Path) -> Path:
    if not dest.exists(): return dest
    stem, suf = dest.stem, dest.suffix; i = 1
    while True:
        cand = dest.parent / f"{stem}_{i}{suf}"
        if not cand.exists(): return cand
        i += 1

def _finalise(tmp: Path, fname: str) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    dest = _safe_dest(OUTPUT_DIR / fname)
    tmp.rename(dest); return dest

# ══════════════════════════════════════════════════════════════
# SIDECAR  (chunk map survives server restarts)
# ══════════════════════════════════════════════════════════════
def _sc_path(tmp: Path) -> Path: return tmp.with_suffix(".chunks.json")

def _sc_load(tmp: Path) -> dict:
    p = _sc_path(tmp)
    try:
        if p.exists(): return json.loads(p.read_text(encoding="utf-8"))
    except Exception: pass
    return {}

def _sc_save(tmp: Path, meta: dict):
    try: _sc_path(tmp).write_text(json.dumps(meta, separators=(",",":")), encoding="utf-8")
    except Exception as e: log.warning(f"Sidecar write: {e}")

def _sc_delete(tmp: Path):
    try: _sc_path(tmp).unlink(missing_ok=True)
    except Exception: pass

def _cleanup_stale_tmp():
    if not TMP_DIR.exists(): return
    kept = deleted = 0
    for f in list(TMP_DIR.iterdir()):
        if not f.is_file(): continue
        if f.suffix == ".tmp":
            if _sc_path(f).exists(): kept += 1
            else:
                try: f.unlink(); deleted += 1
                except Exception: pass
        elif f.name.endswith(".chunks.json"):
            partner = f.with_name(f.name.replace(".chunks.json",""))
            if not partner.exists():
                try: f.unlink()
                except Exception: pass
    if kept:    log.info(f"Resumable .tmp files: {kept}")
    if deleted: log.info(f"Orphaned .tmp deleted: {deleted}")

# ══════════════════════════════════════════════════════════════
# FILE REGISTRY  (filename-based IDs, stable across restarts)
# ══════════════════════════════════════════════════════════════
_registry: Dict[str, Path] = {}
_reg_lock = threading.Lock()
VIDEO_EXTS = {".mp4",".mkv",".avi",".mov",".webm",".m4v",".flv",".wmv"}
AUDIO_EXTS = {".mp3",".flac",".wav",".aac",".ogg",".m4a",".opus"}

def _make_fid(p: Path) -> str:
    return hashlib.md5(p.name.encode()).hexdigest()[:16]

def _registry_refresh():
    new: Dict[str, Path] = {}
    if OUTPUT_DIR.exists():
        for f in sorted(OUTPUT_DIR.iterdir()):
            if not f.is_file() or f.suffix == ".tmp" or not f.exists(): continue
            fid = _make_fid(f)
            if fid in new:
                base = fid; i = 2
                while fid in new: fid = f"{base}_{i}"; i += 1
            new[fid] = f
    with _reg_lock: _registry.clear(); _registry.update(new)

def _get_file(fid: str) -> Optional[Path]:
    with _reg_lock: p = _registry.get(fid)
    if p and p.exists(): return p
    _registry_refresh()
    with _reg_lock: p = _registry.get(fid)
    return p if (p and p.exists()) else None

def _files_list() -> list:
    _registry_refresh()
    result = []
    with _reg_lock: snap = dict(_registry)
    for fid, p in snap.items():
        if not p.exists(): continue
        try:
            ext = p.suffix.lower()
            result.append({"id":fid,"name":p.name,"size":p.stat().st_size,
                           "is_video":ext in VIDEO_EXTS,"is_audio":ext in AUDIO_EXTS})
        except Exception: pass
    return result

def _delete_file(fid: str) -> bool:
    p = _get_file(fid)
    if not p: return False
    try: p.unlink(); log.info(f"Deleted: {p.name}"); _registry_refresh(); return True
    except Exception as e: log.error(f"Delete: {e}"); return False

# ══════════════════════════════════════════════════════════════
# TCP RECEIVER
# ══════════════════════════════════════════════════════════════
def _proto_err(conn, msg):
    log.error(msg)
    try: _send_msg(conn, MSG_ERROR, json.dumps({"status":"error","message":msg}).encode())
    except Exception: pass

def _rx_init(conn, payload):
    m=json.loads(payload); tid=m["transfer_id"]
    fname=Path(m["filename"]).name; fsize=int(m["file_size"])
    nc=int(m["total_chunks"]); csz=int(m["chunk_size"])
    rec=TM.get(tid)
    if rec is None:
        TMP_DIR.mkdir(parents=True,exist_ok=True); tmp=_tmp_path(fname)
        rec=TransferRecord(tid,fname,fsize,nc,csz,tmp,"in","pc"); TM.add(rec)
        _preallocate(tmp,fsize); log.info(f"Recv '{fname}' {fsize/1e9:.2f}GB {nc}ch")
    _send_msg(conn,MSG_ACK,json.dumps({"status":"ok","missing_chunks":rec.missing()}).encode())

def _rx_chunk(conn, payload):
    if len(payload)<44: _proto_err(conn,"chunk too short"); return
    tid=payload[:36].decode(); cidx=struct.unpack("!I",payload[36:40])[0]
    crc=struct.unpack("!I",payload[40:44])[0]; data=payload[44:]
    rec=TM.get(tid)
    if not rec: _proto_err(conn,f"unknown {tid}"); return
    if rec.state==TState.CANCELLED: _send_msg(conn,MSG_ACK,json.dumps({"status":"cancelled"}).encode()); return
    if rec.state==TState.PAUSED:    _send_msg(conn,MSG_ACK,json.dumps({"status":"paused"}).encode()); return
    if _crc32(data)!=crc: _send_msg(conn,MSG_ACK,json.dumps({"status":"retry","chunk_index":cidx}).encode()); return
    _write_chunk_atomic(rec.output_path,cidx*rec.chunk_size,data); rec.mark(cidx,len(data))
    _send_msg(conn,MSG_ACK,json.dumps({"status":"ok","chunk_index":cidx}).encode())

def _rx_finalize(conn, payload):
    m=json.loads(payload); tid=m["transfer_id"]; fcs=m.get("file_checksum","")
    rec=TM.get(tid)
    if not rec: _proto_err(conn,f"unknown {tid}"); return
    if rec.state==TState.CANCELLED: _send_msg(conn,MSG_ACK,json.dumps({"status":"cancelled"}).encode()); return
    miss=rec.missing()
    if miss: _send_msg(conn,MSG_ACK,json.dumps({"status":"incomplete","missing_chunks":miss}).encode()); return
    if fcs and _sha256_file(rec.output_path)!=fcs: _proto_err(conn,"SHA-256 mismatch"); rec.transition(TState.FAILED); return
    dest=_finalise(rec.output_path,rec.filename); el=rec.elapsed(); spd=rec.file_size/el/1e6
    log.info(f"Recv done '{rec.filename}' {el:.1f}s {spd:.1f}MB/s")
    rec.output_path=dest; rec.transition(TState.DONE); TM.remove(tid); _registry_refresh()
    _send_msg(conn,MSG_ACK,json.dumps({"status":"complete","filename":dest.name}).encode())

def _rx_cancel(conn, payload):
    tid=payload[:36].decode() if len(payload)>=36 else ""
    rec=TM.get(tid)
    if rec: rec.cancel(); TM.remove(tid)
    _send_msg(conn,MSG_ACK,json.dumps({"status":"ok"}).encode())

def _rx_pause(conn, payload):
    tid=payload[:36].decode() if len(payload)>=36 else ""
    rec=TM.get(tid); ok=rec.pause() if rec else False
    _send_msg(conn,MSG_ACK,json.dumps({"status":"paused" if ok else "error"}).encode())

def _rx_resume(conn, payload):
    tid=payload[:36].decode() if len(payload)>=36 else ""
    rec=TM.get(tid); ok=rec.resume() if rec else False
    _send_msg(conn,MSG_ACK,json.dumps({"status":"ok" if ok else "error"}).encode())

def _handle_conn(conn, addr):
    try:
        while True:
            try: mtype,payload=_recv_msg(conn)
            except ConnectionError: break
            if   mtype==MSG_INIT:     _rx_init(conn,payload)
            elif mtype==MSG_CHUNK:    _rx_chunk(conn,payload)
            elif mtype==MSG_FINALIZE: _rx_finalize(conn,payload); break
            elif mtype==MSG_CANCEL:   _rx_cancel(conn,payload);   break
            elif mtype==MSG_PAUSE:    _rx_pause(conn,payload)
            elif mtype==MSG_RESUME:   _rx_resume(conn,payload)
            else: _proto_err(conn,f"unknown {mtype:#x}"); break
    except Exception as e: log.error(f"Conn {addr}: {e}")
    finally: conn.close()

def run_tcp_server():
    srv=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
    try: srv.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,TCP_BUF)
    except Exception: pass
    srv.bind(("0.0.0.0",TRANSFER_PORT)); srv.listen(128); srv.settimeout(1.0)
    log.info(f"TCP :{TRANSFER_PORT}")
    while not _shutdown.is_set():
        try:
            conn,addr=srv.accept()
            conn.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
            try: conn.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,TCP_BUF)
            except Exception: pass
            threading.Thread(target=_handle_conn,args=(conn,addr),daemon=True).start()
        except socket.timeout: continue
        except Exception as e:
            if not _shutdown.is_set(): log.error(f"Accept: {e}")
    srv.close()

# ══════════════════════════════════════════════════════════════
# TCP SENDER
# ══════════════════════════════════════════════════════════════
def _worker(chunks, fp, rec, host):
    rem=list(chunks)
    for attempt in range(MAX_RETRIES):
        if not rem: break
        s=None
        try:
            s=_tcp_sock(host,TRANSFER_PORT)
            for cidx in list(rem):
                rec.check(); off=cidx*CHUNK_SIZE; length=min(CHUNK_SIZE,rec.file_size-off)
                with open(fp,"rb") as fh: fh.seek(off); data=fh.read(length)
                crc=_crc32(data)
                pld=rec.transfer_id.encode()+struct.pack("!II",cidx,crc)+data; del data
                _send_msg(s,MSG_CHUNK,pld); del pld
                _,rp=_recv_msg(s); resp=json.loads(rp); st=resp.get("status","")
                if st=="cancelled": raise XferError("cancelled")
                if st=="retry": raise RuntimeError(f"CRC chunk {cidx}")
                if st=="paused": raise RuntimeError("receiver paused")
                rec.mark(cidx,length); rem.remove(cidx)
            break
        except XferError: raise
        except Exception as e:
            if s:
                try: s.close()
                except Exception: pass; s=None
            if attempt<MAX_RETRIES-1: rec.check(); time.sleep(RETRY_DELAY*(attempt+1))
            else: raise RuntimeError(f"worker fail {rem}: {e}") from e
        finally:
            if s:
                try: s.close()
                except Exception: pass

def _ctrl(host, tid, mtype):
    try:
        s=_tcp_sock(host,TRANSFER_PORT,timeout=10)
        try: _send_msg(s,mtype,tid.encode()); _recv_msg(s)
        finally: s.close()
        return True
    except Exception as e: log.warning(f"ctrl {mtype:#x}: {e}"); return False

def send_file(fp: Path, host: str, tid: str = None):
    if not fp.exists(): raise FileNotFoundError(str(fp))
    fsize=fp.stat().st_size; tid=tid or str(uuid.uuid4())
    nc=max(1,(fsize+CHUNK_SIZE-1)//CHUNK_SIZE); fname=fp.name
    log.info(f"Sending '{fname}' {fsize/1e9:.3f}GB -> {host}")
    rec=TransferRecord(tid,fname,fsize,nc,CHUNK_SIZE,fp,"out","pc"); rec.peer_ip=host; TM.add(rec)
    cs={}; ct=threading.Thread(target=lambda: cs.update({"v":_sha256_file(fp)}),daemon=True); ct.start()
    meta=json.dumps({"transfer_id":tid,"filename":fname,"file_size":fsize,
                     "total_chunks":nc,"chunk_size":CHUNK_SIZE}).encode()
    s=_tcp_sock(host,TRANSFER_PORT)
    try: _send_msg(s,MSG_INIT,meta); _,rp=_recv_msg(s)
    finally: s.close()
    missing=json.loads(rp)["missing_chunks"]
    if missing:
        wcs=[missing[i::THREADS] for i in range(THREADS)]; wcs=[w for w in wcs if w]
        with ThreadPoolExecutor(max_workers=len(wcs)) as pool:
            futs=[pool.submit(_worker,w,fp,rec,host) for w in wcs]
            for fut in as_completed(futs):
                if rec.state==TState.CANCELLED:
                    for f in futs: f.cancel(); break
                try: fut.result()
                except XferError:
                    for f in futs: f.cancel(); break
                except Exception as e:
                    log.error(f"Worker: {e}"); rec.transition(TState.FAILED)
                    for f in futs: f.cancel(); break
    if rec.state in (TState.CANCELLED,TState.FAILED): _ctrl(host,tid,MSG_CANCEL); TM.remove(tid); return
    ct.join()
    s=_tcp_sock(host,TRANSFER_PORT)
    try:
        _send_msg(s,MSG_FINALIZE,json.dumps({"transfer_id":tid,"file_checksum":cs.get("v","")}).encode())
        _,rp=_recv_msg(s)
    finally: s.close()
    if json.loads(rp).get("status")=="complete": rec.transition(TState.DONE); log.info(f"Sent '{fname}'")
    else: rec.transition(TState.FAILED)
    TM.remove(tid)

def pause_send(tid,host): r=TM.get(tid); (r and r.pause() and _ctrl(host,tid,MSG_PAUSE))
def resume_send(tid,host): r=TM.get(tid); (r and r.resume() and _ctrl(host,tid,MSG_RESUME))
def cancel_send(tid,host): r=TM.get(tid); (r and r.cancel() and _ctrl(host,tid,MSG_CANCEL))

# ══════════════════════════════════════════════════════════════
# UPLOAD PLAN  (server-dictated immutable chunk parameters)
# GET /api/upload-plan?fname=X&fsize=N
# Returns: {tid, chunk_size, total, par, received, fname, fsize}
# Same fname+fsize always returns same tid if sidecar exists (resume).
# ══════════════════════════════════════════════════════════════
def _choose_chunk_size(fsize: int) -> int:
    if fsize < 200*1024*1024:  return MOB_SMALL
    if fsize < 1024*1024*1024: return MOB_MEDIUM
    return MOB_LARGE

def _detect_par(ua: str) -> int:
    u = ua.lower()
    if "iphone" in u or "ipad" in u: return 2
    return 3

def handle_upload_plan(handler):
    parsed=urlparse(handler.path); qs=parse_qs(parsed.query)
    fname_raw=unquote(qs.get("fname",[""])[0]).strip()
    fsize_raw=qs.get("fsize",["0"])[0]
    ua=handler.headers.get("User-Agent","")
    try: fsize=int(fsize_raw)
    except Exception: fsize=0
    fname=fname_raw.strip().lstrip(".")
    for ch in r'"/\:*?<>|': fname=fname.replace(ch,"_")
    if not fname: fname="upload"
    if fsize<=0:
        handler._respond(400,"application/json",json.dumps({"error":"fsize required"}).encode()); return
    chunk_size=_choose_chunk_size(fsize)
    total=max(1,(fsize+chunk_size-1)//chunk_size)
    par=_detect_par(ua)
    tmp=_tmp_path(fname); sc=_sc_load(tmp)
    if (sc.get("fname")==fname and sc.get("fsize")==fsize
            and sc.get("chunk_size")==chunk_size
            and tmp.exists() and tmp.stat().st_size==fsize):
        tid=sc["tid"]; rec=TM.get(tid)
        if rec is None:
            rec=TransferRecord(tid,fname,fsize,total,chunk_size,tmp,"in","mobile")
            for ci in sc.get("chunks",[]): rec.mark(ci,min(chunk_size,fsize-ci*chunk_size))
            TM.add(rec)
            log.info(f"Plan: resuming '{fname}' {rec.received_count()}/{total} chunks")
        received=rec.received_list()
    else:
        tid=str(uuid.uuid4())
        if tmp.exists():
            try: tmp.unlink()
            except Exception: pass
        _sc_delete(tmp); received=[]
        log.info(f"Plan: new '{fname}' {fsize/1e6:.1f}MB chunk={chunk_size//1024//1024}MB total={total} par={par}")
    plan={"tid":tid,"chunk_size":chunk_size,"total":total,"par":par,
          "received":received,"fname":fname,"fsize":fsize}
    handler._ok("application/json",json.dumps(plan).encode())

# ══════════════════════════════════════════════════════════════
# MOBILE CHUNK UPLOAD
# POST /api/mobile-chunk
# X-CRC32: hex CRC32 of chunk body — server verifies, rejects with 409
# X-Chunk-Size: plan chunk_size (immutable) for correct offset
# ══════════════════════════════════════════════════════════════
def handle_mobile_chunk(handler):
    try:
        tid     =handler.headers.get("X-Transfer-Id","").strip()
        cidx    =int(handler.headers.get("X-Chunk-Index","0"))
        total   =int(handler.headers.get("X-Total-Chunks","1"))
        fsize   =int(handler.headers.get("X-File-Size","0"))
        plan_cs =int(handler.headers.get("X-Chunk-Size","0"))
        crc_hdr =handler.headers.get("X-CRC32","").strip()
        fname_b64=handler.headers.get("X-Filename-B64","").strip()
        if fname_b64:
            try: fname=Path(base64.b64decode(fname_b64).decode("utf-8")).name
            except Exception: fname="upload"
        else: fname=Path(handler.headers.get("X-Filename","upload")).name
        fname=fname.strip().lstrip(".")
        for ch in r'"/\:*?<>|': fname=fname.replace(ch,"_")
        if not fname: fname="upload"
        if not tid: handler._respond(400,"application/json",json.dumps({"error":"no tid"}).encode()); return
        if plan_cs<=0: plan_cs=_choose_chunk_size(fsize)
        rec=TM.get(tid)
        if rec and rec.state==TState.CANCELLED:
            handler._ok("application/json",json.dumps({"status":"cancelled"}).encode()); return
        if rec and rec.state==TState.PAUSED:
            handler._ok("application/json",json.dumps({"status":"paused"}).encode()); return
        try: handler.connection.settimeout(120)
        except Exception: pass
        cl=handler.headers.get("Content-Length","").strip(); data=bytearray()
        try:
            if cl:
                rem=int(cl)
                while rem>0:
                    blk=handler.rfile.read(min(65536,rem))
                    if not blk: break
                    data.extend(blk); rem-=len(blk)
            else:
                while True:
                    blk=handler.rfile.read(65536)
                    if not blk: break
                    data.extend(blk)
        except (ConnectionResetError,BrokenPipeError) as e:
            log.info(f"Chunk {cidx} client disconnected mid-read: {e}"); return
        except Exception as e:
            log.warning(f"Chunk {cidx} read err: {e}")
            handler._respond(500,"application/json",json.dumps({"error":"read_timeout"}).encode()); return
        data=bytes(data)
        if not data: handler._respond(400,"application/json",json.dumps({"error":f"empty {cidx}"}).encode()); return
        # CRC32 verification
        if crc_hdr:
            try: expected=int(crc_hdr,16)
            except Exception: expected=-1
            if _crc32(data)!=expected:
                log.warning(f"CRC mismatch chunk {cidx}")
                handler._respond(409,"application/json",json.dumps({"status":"crc_error","retry":cidx}).encode()); return
        TMP_DIR.mkdir(parents=True,exist_ok=True); tmp=_tmp_path(fname)
        if rec is None:
            with TM.creation_lock(tid):
                rec=TM.get(tid)
                if rec is None:
                    sc=_sc_load(tmp)
                    if (sc.get("tid")==tid and sc.get("fname")==fname
                            and sc.get("fsize")==fsize and sc.get("chunk_size")==plan_cs
                            and tmp.exists() and tmp.stat().st_size==fsize):
                        rec=TransferRecord(tid,fname,fsize,total,plan_cs,tmp,"in","mobile")
                        for ci in sc.get("chunks",[]): rec.mark(ci,min(plan_cs,fsize-ci*plan_cs))
                        TM.add(rec); log.info(f"Restored '{fname}' {rec.received_count()}/{total}")
                    else:
                        if tmp.exists():
                            try: tmp.unlink()
                            except Exception: pass
                        _sc_delete(tmp)
                        rec=TransferRecord(tid,fname,fsize,total,plan_cs,tmp,"in","mobile"); TM.add(rec)
                        _preallocate(tmp,fsize)
                        log.info(f"Mobile '{fname}' {fsize/1e6:.1f}MB {total}ch @{plan_cs//1024//1024}MB")
        if rec.cancel_ev.is_set(): handler._ok("application/json",json.dumps({"status":"cancelled"}).encode()); return
        # Idempotent
        if cidx in rec._received:
            handler._ok("application/json",json.dumps({"status":"ok","received":cidx}).encode()); return
        offset=cidx*plan_cs
        _write_chunk_atomic(tmp,offset,data); nbytes=len(data); del data; rec.mark(cidx,nbytes)
        try:
            _sc_save(tmp,{"tid":tid,"fname":fname,"fsize":fsize,
                          "chunk_size":plan_cs,"chunks":rec.received_list()})
        except Exception: pass
        if rec.complete:
            if not rec.bytes_ok():
                log.error(f"Bytes mismatch '{fname}': wrote {rec._bytes_written} expected {fsize}")
                rec.transition(TState.FAILED)
                try: tmp.unlink(missing_ok=True)
                except Exception: pass
                _sc_delete(tmp); TM.remove(tid)
                handler._respond(500,"application/json",json.dumps({"error":"bytes_mismatch"}).encode()); return
            el=rec.elapsed(); spd=fsize/el/1e6
            log.info(f"Mobile done '{fname}' {el:.1f}s {spd:.1f}MB/s")
            dest=_finalise(tmp,fname); _sc_delete(tmp)
            rec.output_path=dest; rec.transition(TState.DONE); TM.remove(tid); _registry_refresh()
            handler._ok("application/json",json.dumps({"status":"complete"}).encode())
        else:
            handler._ok("application/json",json.dumps({"status":"ok","received":cidx}).encode())
    except (ConnectionResetError,BrokenPipeError) as e:
        log.info(f"Chunk {handler.headers.get('X-Chunk-Index','?')} disconnected: {e}")
    except Exception as e:
        log.error(f"Mobile chunk: {e}",exc_info=True)
        try: handler._respond(500,"application/json",json.dumps({"error":str(e)}).encode())
        except Exception: pass

def handle_mobile_finalize(handler):
    try:
        length=int(handler.headers.get("Content-Length",0))
        body=json.loads(handler.rfile.read(length).decode())
        tid=body.get("tid",""); rec=TM.get(tid)
        if not rec: handler._ok("application/json",json.dumps({"status":"already_done"}).encode()); return
        if not rec.complete:
            handler._ok("application/json",json.dumps({"status":"incomplete","missing":rec.missing()}).encode()); return
        if not rec.bytes_ok():
            rec.transition(TState.FAILED)
            try: rec.output_path.unlink(missing_ok=True)
            except Exception: pass
            _sc_delete(rec.output_path); TM.remove(tid)
            handler._respond(500,"application/json",json.dumps({"error":"bytes_mismatch"}).encode()); return
        if rec.output_path.suffix==".tmp":
            dest=_finalise(rec.output_path,rec.filename); _sc_delete(rec.output_path)
            rec.output_path=dest; rec.transition(TState.DONE); TM.remove(tid); _registry_refresh()
        handler._ok("application/json",json.dumps({"status":"complete"}).encode())
    except Exception as e:
        log.error(f"Finalize: {e}",exc_info=True)
        try: handler._respond(500,"application/json",json.dumps({"error":str(e)}).encode())
        except Exception: pass

# ══════════════════════════════════════════════════════════════
# HTTP STREAMING  — direct or FFmpeg transcode
# ══════════════════════════════════════════════════════════════
def serve_ranged(handler, fpath: Path, inline: bool = False, audio_index: int = 0):
    if not fpath.exists(): handler._respond(404,"text/plain",b"Not found"); return
    if inline and FFMPEG and _needs_transcode(fpath):
        _serve_transcode(handler, fpath, audio_index)
    else:
        _serve_direct(handler, fpath, inline)

def _serve_direct(handler, fpath: Path, inline: bool):
    mime=mimetypes.guess_type(str(fpath))[0] or "application/octet-stream"
    size=fpath.stat().st_size; rng=handler.headers.get("Range","").strip()
    start,end,status=0,size-1,200
    if rng and rng.startswith("bytes="):
        try:
            p=rng[6:].split("-")
            if p[0]: start=int(p[0])
            if len(p)>1 and p[1]: end=int(p[1])
            status=206
        except Exception: pass
    end=min(end,size-1); length=end-start+1
    handler.send_response(status)
    handler.send_header("Content-Type",mime); handler.send_header("Content-Length",str(length))
    handler.send_header("Accept-Ranges","bytes"); handler.send_header("Cache-Control","no-cache")
    if status==206: handler.send_header("Content-Range",f"bytes {start}-{end}/{size}")
    if not inline: handler.send_header("Content-Disposition",f'attachment; filename="{fpath.name}"')
    handler.end_headers()
    try:
        with open(fpath,"rb") as f:
            f.seek(start); rem=length
            while rem>0:
                blk=f.read(min(STREAM_BLOCK,rem))
                if not blk: break
                handler.wfile.write(blk); rem-=len(blk)
    except (BrokenPipeError,ConnectionResetError): pass

def _serve_transcode(handler, fpath: Path, audio_index: int = 0):
    """
    Transcode to H.264/AAC in MPEG-TS via FFmpeg pipe.
    Uses chunked transfer encoding — no Content-Length needed.
    FIX: BaseHTTPServer requires us to manually write HTTP/1.1 chunked response
    because send_header("Transfer-Encoding","chunked") alone isn't enough —
    Python's wfile doesn't add chunk framing automatically.
    """
    ai=max(0,audio_index)
    cmd=[FFMPEG,"-loglevel","error","-i",str(fpath),
         "-map","0:v:0","-map",f"0:a:{ai}",
         "-c:v","libx264","-preset","fast","-crf","23",
         "-c:a","aac","-ac","2","-b:a","192k",
         "-f","mpegts","pipe:1"]
    proc=None
    try:
        handler.send_response(200)
        handler.send_header("Content-Type","video/mp2t")
        handler.send_header("Cache-Control","no-cache")
        handler.send_header("X-Transcoded","1")
        # Use chunked TE — write raw without Content-Length
        handler.send_header("Transfer-Encoding","chunked")
        handler.end_headers()
        proc=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                              bufsize=TRANSCODE_BLOCK)
        while True:
            blk=proc.stdout.read(TRANSCODE_BLOCK)
            if not blk: break
            try:
                # Write chunked framing manually
                handler.wfile.write(f"{len(blk):X}\r\n".encode()+blk+b"\r\n")
                handler.wfile.flush()
            except (BrokenPipeError,ConnectionResetError): break
        try: handler.wfile.write(b"0\r\n\r\n"); handler.wfile.flush()
        except Exception: pass
    except (BrokenPipeError,ConnectionResetError): pass
    except Exception as e: log.error(f"Transcode: {e}")
    finally:
        if proc:
            try: proc.kill(); proc.wait(timeout=3)
            except Exception: pass

def srt_to_vtt(s: str) -> str:
    NL="\n"; out=["WEBVTT"+NL]
    for blk in s.strip().split(NL+NL):
        pts=blk.strip().split(NL)
        if len(pts)<3: continue
        out.append(NL+pts[1].replace(",",".")+NL+NL.join(pts[2:]))
    return NL.join(out)

# ══════════════════════════════════════════════════════════════
# DEVICE DISCOVERY
# ══════════════════════════════════════════════════════════════
_discovered: Dict[str,dict]={}; _disc_lk=threading.Lock()

def run_discovery():
    my_ip=get_local_ip(); hn=socket.gethostname()
    rs=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    rs.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); rs.settimeout(1.0)
    try: rs.bind(("0.0.0.0",DISCOVERY_PORT))
    except Exception: pass
    bs=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    bs.setsockopt(socket.SOL_SOCKET,socket.SO_BROADCAST,1); bs.settimeout(1.0)
    msg=json.dumps({"v":6,"ip":my_ip,"port":UI_PORT,"name":hn}).encode()
    def _bc():
        while not _shutdown.is_set():
            try: bs.sendto(msg,("<broadcast>",DISCOVERY_PORT))
            except Exception:
                try: bs.sendto(msg,("255.255.255.255",DISCOVERY_PORT))
                except Exception: pass
            _shutdown.wait(5.0)
    threading.Thread(target=_bc,daemon=True).start()
    while not _shutdown.is_set():
        try:
            data,(src_ip,_)=rs.recvfrom(1024)
            if src_ip==my_ip: continue
            info=json.loads(data.decode())
            with _disc_lk:
                _discovered[src_ip]={"ip":src_ip,"name":info.get("name",src_ip),
                                      "port":info.get("port",8080),"last_seen":time.time()}
        except socket.timeout:
            now=time.time()
            with _disc_lk:
                stale=[ip for ip,d in _discovered.items() if now-d["last_seen"]>20]
                for ip in stale: del _discovered[ip]
        except Exception: pass

def get_local_ip():
    try:
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8",80)); return s.getsockname()[0]
    except Exception: return "127.0.0.1"

def get_devices():
    now=time.time()
    with _disc_lk: return [d for d in _discovered.values() if now-d["last_seen"]<20]

def _is_mobile(ua):
    u=ua.lower()
    return any(k in u for k in ("iphone","ipad","android","mobile","tablet"))

def _run_send_job(job):
    fp=Path(job["path"]); tid=job.get("tid",str(uuid.uuid4())); is_tmp=job.get("is_tmp",False)
    try: send_file(fp,job["peer_ip"],tid)
    except Exception as e: log.error(f"Send job: {e}")
    finally:
        if is_tmp:
            try: fp.unlink()
            except Exception: pass

# ══════════════════════════════════════════════════════════════
# HTML — DESKTOP UI
# ══════════════════════════════════════════════════════════════
HTML_DESKTOP = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SwiftDrop</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Syne:wght@700;800&family=DM+Sans:wght@400;500&display=swap');
:root{--bg:#08080f;--s1:#111118;--s2:#18181f;--bd:#26263a;--acc:#6c63ff;--acc2:#ff6584;--grn:#00e5a0;--ylw:#ffd166;--txt:#e8e8f4;--mut:#6b6b8a;--r:14px}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font-family:'DM Sans',sans-serif;min-height:100vh;padding:20px;background-image:radial-gradient(ellipse 60% 40% at 15% 15%,rgba(108,99,255,.09),transparent 70%),radial-gradient(ellipse 50% 35% at 85% 85%,rgba(255,101,132,.06),transparent 70%)}
header{display:flex;align-items:center;justify-content:space-between;max-width:1200px;margin:0 auto 24px;flex-wrap:wrap;gap:12px}
.logo{font-family:'Syne',sans-serif;font-size:1.5rem;font-weight:800}.logo span{color:var(--acc)}.logo sup{font-size:.58rem;color:var(--mut);font-weight:400}
.hr{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.pill{background:var(--s2);border:1px solid var(--bd);border-radius:40px;padding:6px 14px;font-size:.78rem;display:flex;align-items:center;gap:7px}
.dot{width:7px;height:7px;border-radius:50%;background:var(--grn);box-shadow:0 0 6px var(--grn);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.bexit{background:rgba(255,101,132,.1);border:1px solid rgba(255,101,132,.3);color:var(--acc2);border-radius:40px;padding:6px 16px;font-family:'Syne',sans-serif;font-size:.78rem;font-weight:700;cursor:pointer}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;max-width:1200px;margin:0 auto}
@media(max-width:900px){.grid{grid-template-columns:1fr 1fr}}@media(max-width:560px){.grid{grid-template-columns:1fr}}
.tw{grid-column:1/-1}.card{background:var(--s1);border:1px solid var(--bd);border-radius:var(--r);padding:22px}
.ct{font-family:'Syne',sans-serif;font-weight:700;font-size:.95rem;margin-bottom:16px;display:flex;align-items:center;gap:8px}
.bx{font-size:.62rem;padding:2px 9px;border-radius:18px;font-weight:700}
.bv{background:rgba(108,99,255,.2);color:var(--acc)}.bg2{background:rgba(0,229,160,.15);color:var(--grn)}.by{background:rgba(255,209,102,.15);color:var(--ylw)}
#dz{border:2px dashed var(--bd);border-radius:11px;padding:28px 16px;text-align:center;cursor:pointer;transition:all .18s;background:var(--s2)}
#dz:hover,#dz.over{border-color:var(--acc);background:rgba(108,99,255,.07)}
#dz em{font-size:2.2rem;font-style:normal;display:block;margin-bottom:8px}
#dz p{color:var(--mut);font-size:.84rem;line-height:1.6}#dz strong{color:var(--txt)}
#fi{display:none}
#sf{margin-top:10px;padding:8px 12px;background:rgba(108,99,255,.12);border:1px solid rgba(108,99,255,.3);border-radius:8px;font-size:.8rem;display:none;word-break:break-all}
.lbl{display:block;font-size:.77rem;color:var(--mut);margin:12px 0 4px}
input[type=text]{width:100%;background:var(--s2);border:1px solid var(--bd);border-radius:9px;padding:9px 12px;color:var(--txt);font-family:'DM Sans',sans-serif;font-size:.84rem;outline:none;transition:border .18s}
input[type=text]:focus{border-color:var(--acc)}input::placeholder{color:var(--mut)}
.bsend{width:100%;margin-top:14px;padding:12px;border:none;border-radius:9px;cursor:pointer;font-family:'Syne',sans-serif;font-weight:700;font-size:.9rem;background:var(--acc);color:#fff}
.bsend:hover{background:#7c74ff}.bsend:disabled{background:var(--bd);color:var(--mut);cursor:not-allowed}
.ipbox{margin-top:16px;background:var(--s2);border:1px solid var(--bd);border-radius:9px;padding:14px;font-family:monospace;font-size:1.1rem;color:var(--grn);cursor:pointer;position:relative}
.toast{position:absolute;top:-26px;left:50%;transform:translateX(-50%);background:var(--grn);color:#000;font-size:.68rem;font-weight:700;padding:2px 10px;border-radius:5px;opacity:0;transition:opacity .18s;pointer-events:none;white-space:nowrap}
.murl{font-family:monospace;font-size:.88rem;color:var(--acc2);background:var(--s2);border:1px solid var(--bd);border-radius:9px;padding:12px;margin-top:12px;word-break:break-all;cursor:pointer;position:relative}
.mt2{position:absolute;top:-26px;left:50%;transform:translateX(-50%);background:var(--acc2);color:#fff;font-size:.68rem;font-weight:700;padding:2px 10px;border-radius:5px;opacity:0;transition:opacity .18s;pointer-events:none;white-space:nowrap}
.hint{font-size:.7rem;color:var(--mut);margin-top:6px}
.snote{margin-top:14px;font-size:.76rem;color:var(--mut);background:var(--s2);border-radius:7px;padding:9px 11px;line-height:1.6}
.sec{font-family:'Syne',sans-serif;font-weight:700;font-size:.85rem;color:var(--mut);margin-bottom:12px;letter-spacing:.05em}
.ti{background:var(--s2);border:1px solid var(--bd);border-radius:11px;padding:14px 16px;margin-bottom:9px;animation:si .22s ease}
@keyframes si{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:none}}
.th{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.tn{font-weight:500;font-size:.84rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:46%}
.tm{font-size:.72rem;color:var(--mut)}
.pbg{height:4px;background:var(--bd);border-radius:2px;overflow:hidden;margin-bottom:5px}
.bar{height:100%;border-radius:2px;transition:width .35s;min-width:2px}
.bac{background:linear-gradient(90deg,var(--acc),#a78bfa)}.bgr{background:linear-gradient(90deg,var(--grn),#00b4d8)}.bmo{background:linear-gradient(90deg,var(--acc2),#ff9a3c)}.bdn{background:var(--grn)}.bfl{background:var(--acc2)}.bpa{background:var(--ylw)}
.tf{display:flex;justify-content:space-between;align-items:center;font-size:.71rem;color:var(--mut)}
.tcc{display:flex;gap:4px;margin-left:5px}
.tcc button{border:none;border-radius:4px;padding:2px 8px;font-size:.68rem;font-family:'Syne',sans-serif;font-weight:700;cursor:pointer}
.tbp{background:rgba(255,209,102,.15);color:var(--ylw);border:1px solid rgba(255,209,102,.3)}
.tbr{background:rgba(0,229,160,.12);color:var(--grn);border:1px solid rgba(0,229,160,.3)}
.tbc{background:rgba(255,101,132,.12);color:var(--acc2);border:1px solid rgba(255,101,132,.3)}
.ok{color:var(--grn);font-weight:600}.er{color:var(--acc2)}.pa{color:var(--ylw);font-weight:600}
.empty{text-align:center;padding:22px;color:var(--mut);font-size:.82rem}
.frow{display:flex;align-items:center;gap:8px;padding:9px 0;border-bottom:1px solid var(--bd);font-size:.82rem}
.frow:last-child{border-bottom:none;padding-bottom:0}
.ficon{font-size:1.1rem;flex-shrink:0}.fname{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}.fsz{color:var(--mut);font-size:.74rem;white-space:nowrap;flex-shrink:0}
.fbtns{display:flex;gap:5px;flex-shrink:0}
.bwat{background:var(--acc);color:#fff;border:none;border-radius:6px;padding:4px 10px;font-size:.72rem;font-family:'Syne',sans-serif;font-weight:700;text-decoration:none;display:inline-block}
.bdl{background:var(--s2);color:var(--txt);border:1px solid var(--bd);border-radius:6px;padding:4px 9px;font-size:.72rem;font-family:'Syne',sans-serif;font-weight:700;text-decoration:none;display:inline-block}
.bdel{background:rgba(255,101,132,.1);color:var(--acc2);border:1px solid rgba(255,101,132,.3);border-radius:6px;padding:4px 9px;font-size:.72rem;font-family:'Syne',sans-serif;font-weight:700;cursor:pointer}
.bdel:hover{background:rgba(255,101,132,.3)}
.drow{display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--bd);font-size:.82rem}
.drow:last-child{border-bottom:none;padding-bottom:0}
.dip{font-family:monospace;color:var(--acc);font-size:.84rem}.dname{color:var(--txt);font-weight:500}
.bsnd{background:rgba(108,99,255,.15);color:var(--acc);border:1px solid rgba(108,99,255,.3);border-radius:6px;padding:3px 10px;font-size:.72rem;font-family:'Syne',sans-serif;font-weight:700;cursor:pointer}
#modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:999;align-items:center;justify-content:center;backdrop-filter:blur(4px)}
#modal.show{display:flex}
.mbox{background:var(--s1);border:1px solid var(--bd);border-radius:var(--r);padding:30px 24px;max-width:340px;width:90%;text-align:center}
.mbox h2{font-family:'Syne',sans-serif;font-size:1.1rem;margin-bottom:9px}
.mbox p{color:var(--mut);font-size:.84rem;line-height:1.6;margin-bottom:22px}
.mbts{display:flex;gap:9px}
.mbts button{flex:1;padding:10px;border-radius:9px;border:none;cursor:pointer;font-family:'Syne',sans-serif;font-weight:700;font-size:.84rem}
.mc2{background:var(--s2);color:var(--txt);border:1px solid var(--bd)}.mx2{background:var(--acc2);color:#fff}
</style></head>
<body>
<header>
  <div class="logo">Swift<span>Drop</span><sup>v6</sup></div>
  <div class="hr">
    <div class="pill"><div class="dot"></div><span>IP:&nbsp;<strong id="myip">...</strong></span></div>
    <button class="bexit" onclick="showExit()">Exit</button>
  </div>
</header>
<div class="grid">
  <div class="card">
    <div class="ct">Send to PC <span class="bx bv">PC-to-PC</span></div>
    <div id="dz" onclick="document.getElementById('fi').click()">
      <em>&#128228;</em><p><strong>Click to browse</strong> or drag and drop</p>
      <input type="file" id="fi">
    </div>
    <div id="sf"></div>
    <label class="lbl">Receiver IP</label>
    <input type="text" id="pip" placeholder="192.168.0.x">
    <button class="bsend" id="snd" disabled onclick="doSend()">Send File</button>
  </div>
  <div class="card">
    <div class="ct">Receive / Connect <span class="bx bg2">READY</span></div>
    <p style="color:var(--mut);font-size:.82rem;line-height:1.7">Share your IP with other PCs. Open link on any phone.</p>
    <div class="ipbox" onclick="copyIp()"><span id="ipbig">...</span><div class="toast" id="t1">Copied!</div></div>
    <div class="hint">Click to copy</div>
    <div class="murl" onclick="copyMob()"><div class="mt2" id="t2">Copied!</div><span id="moblink">...</span></div>
    <div class="hint">Open on phone / tablet</div>
    <div class="snote">Files saved in <strong>received/</strong> next to app</div>
  </div>
  <div class="card">
    <div class="ct">Devices <span class="bx by">LIVE</span></div>
    <div id="devlist"><div class="empty">Scanning...</div></div>
  </div>
  <div class="tw"><div class="sec">TRANSFERS</div><div id="tlist"><div class="empty">No transfers yet</div></div></div>
  <div class="tw">
    <div class="sec">FILES <span style="font-size:.7rem;font-weight:400;color:var(--mut)">received/ &middot; &#9654; stream &middot; &#8595; download &middot; &#10005; delete</span></div>
    <div id="filelist"><div class="empty">No files yet</div></div>
    <button onclick="refreshFiles()" style="margin-top:10px;padding:7px 14px;background:var(--s2);border:1px solid var(--bd);border-radius:7px;color:var(--mut);font-size:.77rem;cursor:pointer">Refresh</button>
  </div>
</div>
<div id="modal"><div class="mbox"><h2>Exit SwiftDrop?</h2><p>Active transfers will stop.</p><div class="mbts"><button class="mc2" onclick="hideExit()">Cancel</button><button class="mx2" onclick="doExit()">Exit</button></div></div></div>
<script>
let selFile=null;
const dz=document.getElementById('dz'),fi=document.getElementById('fi');
dz.addEventListener('dragover',e=>{e.preventDefault();dz.classList.add('over');});
dz.addEventListener('dragleave',()=>dz.classList.remove('over'));
dz.addEventListener('drop',e=>{e.preventDefault();dz.classList.remove('over');if(e.dataTransfer.files[0])pick(e.dataTransfer.files[0]);});
fi.addEventListener('change',()=>{if(fi.files[0])pick(fi.files[0]);});
function pick(f){selFile=f;const el=document.getElementById('sf');el.style.display='block';el.innerHTML='<b>'+esc(f.name)+'</b> ('+fmt(f.size)+')';chk();}
document.getElementById('pip').addEventListener('input',chk);
function chk(){document.getElementById('snd').disabled=!(selFile&&document.getElementById('pip').value.trim());}
const _peers={};
function doSend(){
  const ip=document.getElementById('pip').value.trim();if(!selFile||!ip)return;
  const fd=new FormData();fd.append('file',selFile);fd.append('peer_ip',ip);
  const b=document.getElementById('snd');b.disabled=true;b.textContent='Sending...';
  fetch('/api/send',{method:'POST',body:fd}).then(r=>r.json()).then(d=>{b.textContent='Send File';chk();if(d.transfer_id)_peers[d.transfer_id]=ip;if(d.error)alert(d.error);}).catch(()=>{b.textContent='Send File';chk();});
}
document.getElementById('tlist').addEventListener('click',function(e){
  const btn=e.target.closest('button[data-action]');if(!btn)return;
  const act=btn.dataset.action,tid=btn.dataset.tid;
  if(act==='cancel'&&!confirm('Cancel?'))return;
  fetch('/api/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:act,transfer_id:tid,peer_ip:_peers[tid]||''})});
});
setInterval(()=>fetch('/api/progress').then(r=>r.json()).then(renderT).catch(()=>{}),700);
function renderT(items){
  const l=document.getElementById('tlist');
  if(!items.length){l.innerHTML='<div class="empty">No transfers yet</div>';return;}
  const active=new Set(items.map(t=>t.transfer_id));
  l.querySelectorAll('.ti').forEach(el=>{if(!active.has(el.dataset.tid))el.remove();});
  items.forEach(t=>{
    let el=document.getElementById('t-'+t.transfer_id);
    if(!el){el=document.createElement('div');el.className='ti';el.id='t-'+t.transfer_id;el.dataset.tid=t.transfer_id;l.prepend(el);}
    const p=t.pct||0,s=t.status,dn=s==='done',fl=s==='failed',pa=s==='paused',cn=s==='cancelled',ac=s==='in_progress'||s==='queued';
    const dir=t.direction==='out'?'Sending to '+(t.peer_ip||'peer'):'From '+(t.source==='mobile'?'&#128247; Phone':'PC');
    const bc=(fl||cn)?'bfl':dn?'bdn':pa?'bpa':t.direction==='out'?'bac':t.source==='mobile'?'bmo':'bgr';
    const sl=dn?'<span class="ok">Done &#10003;</span>':cn?'<span class="er">Cancelled</span>':fl?'<span class="er">Failed</span>':pa?'<span class="pa">Paused</span>':(t.speed||0)+' MB/s';
    let cc='';
    if(ac)cc='<button class="tbp" data-action="pause" data-tid="'+t.transfer_id+'">II</button><button class="tbc" data-action="cancel" data-tid="'+t.transfer_id+'">&#10005;</button>';
    if(pa)cc='<button class="tbr" data-action="resume" data-tid="'+t.transfer_id+'">&#9654;</button><button class="tbc" data-action="cancel" data-tid="'+t.transfer_id+'">&#10005;</button>';
    el.innerHTML='<div class="th"><div class="tn" title="'+esc(t.filename||'')+'">'+esc(t.filename||'...')+'</div><div class="tm">'+dir+' &middot; '+fmt(t.file_size||0)+'</div></div><div class="pbg"><div class="bar '+bc+'" style="width:'+p+'%"></div></div><div class="tf"><span>'+p.toFixed(1)+'%</span><div style="display:flex;align-items:center;gap:6px">'+sl+'<div class="tcc">'+cc+'</div></div></div>';
  });
}
function refreshFiles(){
  fetch('/api/files').then(r=>r.json()).then(files=>{
    const fl=document.getElementById('filelist');
    if(!files.length){fl.innerHTML='<div class="empty">No files yet</div>';return;}
    fl.innerHTML='';
    files.forEach(f=>{
      const row=document.createElement('div');row.className='frow';
      const ico=f.is_video?'&#127916;':f.is_audio?'&#127925;':'&#128196;';
      const wb=f.is_video?'<a class="bwat" href="/player/'+f.id+'" target="_blank">&#9654;</a>':'';
      row.innerHTML='<span class="ficon">'+ico+'</span><span class="fname" title="'+esc(f.name)+'">'+esc(f.name)+'</span><span class="fsz">'+fmt(f.size)+'</span><div class="fbtns">'+wb+'<a class="bdl" href="/api/download/'+f.id+'" download="'+esc(f.name)+'">&#8595;</a><button class="bdel" data-fid="'+f.id+'" data-name="'+esc(f.name)+'">&#10005;</button></div>';
      fl.appendChild(row);
    });
  }).catch(()=>{});
}
refreshFiles();setInterval(refreshFiles,5000);
document.getElementById('filelist').addEventListener('click',function(e){
  const btn=e.target.closest('button.bdel');if(!btn)return;
  if(!confirm('Delete "'+btn.dataset.name+'"?'))return;
  fetch('/api/delete/'+btn.dataset.fid,{method:'DELETE'}).then(r=>r.json()).then(d=>{if(d.ok)btn.closest('.frow').remove();else alert('Delete failed');}).catch(()=>alert('Delete failed'));
});
function refreshDevices(){
  fetch('/api/devices').then(r=>r.json()).then(devs=>{
    const l=document.getElementById('devlist');
    if(!devs.length){l.innerHTML='<div class="empty">No other SwiftDrop found</div>';return;}
    l.innerHTML='';
    devs.forEach(d=>{const row=document.createElement('div');row.className='drow';const info=document.createElement('div');info.style.flex='1';info.innerHTML='<div class="dname">'+esc(d.name)+'</div><div class="dip">'+esc(d.ip)+'</div>';const btn=document.createElement('button');btn.className='bsnd';btn.textContent='Use IP';btn.addEventListener('click',()=>{document.getElementById('pip').value=d.ip;});row.appendChild(info);row.appendChild(btn);l.appendChild(row);});
  }).catch(()=>{});
}
refreshDevices();setInterval(refreshDevices,5000);
fetch('/api/info').then(r=>r.json()).then(d=>{document.getElementById('myip').textContent=d.ip;document.getElementById('ipbig').textContent=d.ip;document.getElementById('moblink').textContent='http://'+d.ip+':8080/mobile';});
function copyIp(){navigator.clipboard.writeText(document.getElementById('ipbig').textContent).then(()=>{const t=document.getElementById('t1');t.style.opacity='1';setTimeout(()=>t.style.opacity='0',1500);}).catch(()=>{});}
function copyMob(){navigator.clipboard.writeText(document.getElementById('moblink').textContent).then(()=>{const t=document.getElementById('t2');t.style.opacity='1';setTimeout(()=>t.style.opacity='0',1500);}).catch(()=>{});}
function showExit(){document.getElementById('modal').classList.add('show');}
function hideExit(){document.getElementById('modal').classList.remove('show');}
document.getElementById('modal').addEventListener('click',function(e){if(e.target===this)hideExit();});
function doExit(){fetch('/api/exit',{method:'POST'}).finally(()=>{document.body.innerHTML='<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100vh;font-family:Syne,sans-serif;gap:14px"><div style="font-size:3rem">&#128075;</div><div style="font-size:1.3rem;font-weight:800">Exited</div></div>';});}
function fmt(b){if(!b)return'0 B';const u=['B','KB','MB','GB','TB'];let i=0;while(b>=1024&&i<4){b/=1024;i++;}return b.toFixed(i?1:0)+' '+u[i];}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
</script></body></html>
"""

# ══════════════════════════════════════════════════════════════
# HTML — MOBILE UI
# ══════════════════════════════════════════════════════════════
HTML_MOBILE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>SwiftDrop</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Syne:wght@700;800&family=DM+Sans:wght@400;500&display=swap');
:root{--bg:#08080f;--s1:#111118;--s2:#18181f;--bd:#26263a;--acc:#6c63ff;--acc2:#ff6584;--grn:#00e5a0;--ylw:#ffd166;--txt:#e8e8f4;--mut:#6b6b8a;--r:16px}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
body{background:var(--bg);color:var(--txt);font-family:'DM Sans',sans-serif;min-height:100vh;padding:18px 15px 36px;background-image:radial-gradient(ellipse 80% 40% at 50% 0%,rgba(108,99,255,.1),transparent 70%)}
h1{font-family:'Syne',sans-serif;font-size:1.7rem;font-weight:800;text-align:center;margin-bottom:4px}
.sub{text-align:center;color:var(--mut);font-size:.78rem;margin-bottom:18px}
.tabs{display:flex;background:var(--s2);border:1px solid var(--bd);border-radius:11px;padding:3px;margin-bottom:14px;gap:3px}
.tab{flex:1;padding:9px;text-align:center;border-radius:8px;font-family:'Syne',sans-serif;font-weight:700;font-size:.82rem;cursor:pointer;color:var(--mut)}
.tab.on{background:var(--acc);color:#fff}
.pn{display:none}.pn.on{display:block}
.card{background:var(--s1);border:1px solid var(--bd);border-radius:var(--r);padding:20px;margin-bottom:14px}
.ct{font-family:'Syne',sans-serif;font-weight:700;font-size:.92rem;margin-bottom:14px}
#dz{border:2px dashed var(--bd);border-radius:11px;padding:28px 14px;text-align:center;background:var(--s2);cursor:pointer}
#dz.over{border-color:var(--acc)}
#dz em{font-size:2.5rem;display:block;margin-bottom:8px;font-style:normal}
#dz p{color:var(--mut);font-size:.86rem;line-height:1.6}#dz strong{color:var(--txt)}
#fi{display:none}
#sf{margin-top:10px;padding:9px 12px;background:rgba(108,99,255,.12);border:1px solid rgba(108,99,255,.3);border-radius:9px;font-size:.8rem;display:none;word-break:break-all;line-height:1.5}
.bup{width:100%;margin-top:14px;padding:14px;border:none;border-radius:11px;cursor:pointer;font-family:'Syne',sans-serif;font-weight:700;font-size:.96rem;background:var(--acc);color:#fff}
.bup:active{transform:scale(.97)}.bup:disabled{background:var(--bd);color:var(--mut)}
.cr{display:flex;gap:9px;margin-top:11px}
.bc{flex:1;padding:11px;border:none;border-radius:9px;cursor:pointer;font-family:'Syne',sans-serif;font-weight:700;font-size:.84rem}
.bpa{background:rgba(255,209,102,.2);color:var(--ylw);border:1px solid rgba(255,209,102,.4)}
.bca{background:rgba(255,101,132,.15);color:var(--acc2);border:1px solid rgba(255,101,132,.35)}
.pw{display:none;margin-top:14px}
.pl{display:flex;justify-content:space-between;font-size:.79rem;color:var(--mut);margin-bottom:6px}
.pbg{height:7px;background:var(--bd);border-radius:4px;overflow:hidden}
.pb{height:100%;border-radius:4px;background:linear-gradient(90deg,var(--acc),#a78bfa);transition:width .25s;width:0}
.pb.paused{background:linear-gradient(90deg,var(--ylw),#f4a261)}
.ps{text-align:center;font-size:.82rem;margin-top:9px;min-height:1.1em}
.ok{color:var(--grn);font-weight:600}.er{color:var(--acc2)}.pa{color:var(--ylw);font-weight:600}
.warn{background:rgba(255,209,102,.12);border:1px solid rgba(255,209,102,.3);border-radius:8px;padding:10px 12px;font-size:.78rem;color:var(--ylw);line-height:1.55;margin-top:12px}
.irow{display:flex;justify-content:space-between;align-items:center;padding:9px 0;border-bottom:1px solid var(--bd);font-size:.82rem}
.irow:last-child{border-bottom:none;padding-bottom:0}
.ilbl{color:var(--mut)}.ival{font-family:monospace;color:var(--grn);font-weight:500}
.fitem{display:flex;align-items:center;gap:8px;padding:10px 0;border-bottom:1px solid var(--bd);font-size:.82rem}
.fitem:last-child{border-bottom:none;padding-bottom:0}
.fico{font-size:1.1rem;flex-shrink:0}.fname{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
.fsz{color:var(--mut);font-size:.73rem;white-space:nowrap}
.bw{background:var(--acc);color:#fff;border:none;border-radius:7px;padding:5px 11px;font-family:'Syne',sans-serif;font-weight:700;font-size:.74rem;cursor:pointer;text-decoration:none;display:inline-block}
.bdl{background:var(--s2);color:var(--txt);border:1px solid var(--bd);border-radius:7px;padding:5px 9px;font-family:'Syne',sans-serif;font-weight:700;font-size:.74rem;cursor:pointer;text-decoration:none;display:inline-block}
</style></head>
<body>
<h1>Swift<span style="color:var(--acc)">Drop</span></h1>
<div class="sub">v6 &mdash; Upload &middot; Watch &middot; Download</div>
<div class="tabs">
  <div class="tab on" onclick="sw('up')">&#128228; Send</div>
  <div class="tab" onclick="sw('watch')">&#127916; Watch</div>
  <div class="tab" onclick="sw('dl')">&#128229; Get</div>
</div>
<div class="pn on" id="pn-up">
  <div class="card">
    <div class="ct">Upload to PC</div>
    <div id="dz" onclick="document.getElementById('fi').click()">
      <em>&#128193;</em><p><strong>Tap to choose files</strong></p>
      <p style="margin-top:4px;font-size:.77rem">Any size &middot; Resumes on reconnect</p>
      <input type="file" id="fi" multiple>
    </div>
    <div id="sf"></div>
    <button class="bup" id="ubtn" disabled onclick="startUp()">Send to PC</button>
    <div class="cr" id="ctr" style="display:none">
      <button class="bc bpa" id="pbtn" onclick="togPause()">Pause</button>
      <button class="bc bca" onclick="doCancel()">Cancel</button>
    </div>
    <div class="pw" id="pw">
      <div class="pl"><span id="ppct">0%</span><span id="pspd"></span></div>
      <div class="pbg"><div class="pb" id="pbar"></div></div>
      <div class="ps" id="pstat"></div>
    </div>
    <div class="warn" id="lockwarn" style="display:none">
      &#9888;&#65039; Keep screen on during upload.<br>
      If it locks, upload pauses. Reopen to resume — progress is saved.
    </div>
  </div>
  <div class="card">
    <div class="ct">Connection</div>
    <div class="irow"><span class="ilbl">PC IP</span><span class="ival" id="pcip">...</span></div>
    <div class="irow"><span class="ilbl">Protocol</span><span class="ival">HTTP chunk + CRC32</span></div>
    <div class="irow"><span class="ilbl">Saved to</span><span class="ival" style="font-size:.74rem">received/ on PC</span></div>
  </div>
</div>
<div class="pn" id="pn-watch">
  <div class="card">
    <div class="ct">Stream from PC</div>
    <div id="wlist"><div style="text-align:center;padding:20px;color:var(--mut)">Loading...</div></div>
    <button onclick="loadMedia()" style="margin-top:12px;width:100%;padding:10px;background:var(--s2);border:1px solid var(--bd);border-radius:9px;color:var(--mut);font-family:'Syne',sans-serif;font-size:.82rem;cursor:pointer">Refresh</button>
  </div>
</div>
<div class="pn" id="pn-dl">
  <div class="card">
    <div class="ct">Download from PC</div>
    <div id="dlist"><div style="text-align:center;padding:20px;color:var(--mut)">Loading...</div></div>
    <button onclick="loadFiles()" style="margin-top:12px;width:100%;padding:10px;background:var(--s2);border:1px solid var(--bd);border-radius:9px;color:var(--mut);font-family:'Syne',sans-serif;font-size:.82rem;cursor:pointer">Refresh</button>
  </div>
</div>
<script>
function sw(n){['up','watch','dl'].forEach((t,i)=>{document.getElementById('pn-'+t).classList.toggle('on',t===n);document.querySelectorAll('.tab')[i].classList.toggle('on',t===n);});if(n==='watch')loadMedia();if(n==='dl')loadFiles();}
let selFiles=[];const fi=document.getElementById('fi');
fi.addEventListener('change',()=>{selFiles=Array.from(fi.files);if(!selFiles.length)return;const el=document.getElementById('sf');el.style.display='block';el.innerHTML=selFiles.map(f=>'<b>'+esc(f.name)+'</b> ('+fmt(f.size)+')').join('<br>');document.getElementById('ubtn').disabled=false;});
const dz=document.getElementById('dz');
dz.addEventListener('dragover',e=>{e.preventDefault();dz.classList.add('over');});
dz.addEventListener('dragleave',()=>dz.classList.remove('over'));
dz.addEventListener('drop',e=>{e.preventDefault();dz.classList.remove('over');selFiles=Array.from(e.dataTransfer.files);if(selFiles.length){document.getElementById('sf').style.display='block';document.getElementById('sf').innerHTML=selFiles.map(f=>esc(f.name)).join(', ');document.getElementById('ubtn').disabled=false;}});
let _paused=false,_cancelled=false,_acs=[],_curTid=null;
function sleep(ms){return new Promise(r=>setTimeout(r,ms));}
async function waitR(){while(_paused&&!_cancelled)await sleep(250);}
function ulog(msg,isErr=false,isOk=false){const el=document.getElementById('pstat');el.textContent=msg;el.className='ps'+(isErr?' er':isOk?' ok':msg.includes('Paused')?' pa':'');}
// CRC32
const CRC_TABLE=(()=>{const t=new Uint32Array(256);for(let i=0;i<256;i++){let c=i;for(let j=0;j<8;j++)c=c&1?(c>>>1)^0xEDB88320:(c>>>1);t[i]=c;}return t;})();
function crc32buf(buf){const v=new Uint8Array(buf);let c=0xFFFFFFFF;for(let i=0;i<v.length;i++)c=(c>>>8)^CRC_TABLE[(c^v[i])&0xFF];return((c^0xFFFFFFFF)>>>0).toString(16).padStart(8,'0');}
// TID persistence
function tidKey(n,s){return 'sd_tid_'+encodeURIComponent(n)+'_'+s;}
function storeTid(n,s,t){try{sessionStorage.setItem(tidKey(n,s),t);localStorage.setItem(tidKey(n,s),t);}catch(e){}}
function loadTid(n,s){try{return sessionStorage.getItem(tidKey(n,s))||localStorage.getItem(tidKey(n,s))||null;}catch(e){return null;}}
function clearTid(n,s){try{sessionStorage.removeItem(tidKey(n,s));localStorage.removeItem(tidKey(n,s));}catch(e){}}
async function getUploadPlan(file){
  const saved=loadTid(file.name,file.size);
  const url='/api/upload-plan?fname='+encodeURIComponent(file.name)+'&fsize='+file.size+(saved?'&tid='+saved:'');
  for(let i=0;i<5;i++){
    try{const r=await fetch(url);if(r.ok){const p=await r.json();storeTid(file.name,file.size,p.tid);return p;}}catch(e){}
    if(i<4)await sleep(2000*(i+1));
  }
  throw new Error('Cannot reach server');
}
async function upChunk(file,cidx,plan,t0,sentRef){
  const{tid,chunk_size:cs,total}=plan;
  const blob=file.slice(cidx*cs,(cidx+1)*cs);
  const data=await blob.arrayBuffer();
  const crc=crc32buf(data);
  const sfn=btoa(unescape(encodeURIComponent(file.name)));
  const hdrs={'Content-Type':'application/octet-stream','X-Transfer-Id':tid,
    'X-Chunk-Index':String(cidx),'X-Total-Chunks':String(total),
    'X-File-Size':String(file.size),'X-Chunk-Size':String(cs),
    'X-CRC32':crc,'X-Filename-B64':sfn};
  for(let att=0;att<8;att++){
    if(_cancelled)return false; await waitR(); if(_cancelled)return false;
    const ac=new AbortController(); _acs.push(ac);
    const tmr=setTimeout(()=>ac.abort(),300000);
    try{
      const res=await fetch('/api/mobile-chunk',{method:'POST',signal:ac.signal,headers:hdrs,body:new Blob([data])});
      clearTimeout(tmr);_acs=_acs.filter(x=>x!==ac);
      if(res.status===409){ulog('CRC retry chunk '+cidx);await sleep(500);continue;}
      if(!res.ok)throw new Error('HTTP '+res.status);
      const j=await res.json();
      if(j.status==='cancelled'){_cancelled=true;return false;}
      if(j.status==='paused'){await sleep(800);continue;}
      sentRef.v++;
      const secs=(Date.now()-t0)/1000||0.001;
      const spd=(sentRef.v*cs/secs/1e6).toFixed(1);
      document.getElementById('pbar').style.width=Math.min(100,Math.round(sentRef.v/total*100))+'%';
      document.getElementById('ppct').textContent=Math.min(100,Math.round(sentRef.v/total*100))+'%';
      document.getElementById('pspd').textContent=spd+' MB/s';
      return true;
    }catch(e){
      clearTimeout(tmr);_acs=_acs.filter(x=>x!==ac);
      if(_cancelled)return false;
      if(e.name==='AbortError'){ulog('Timeout chunk '+cidx+', retry...');await sleep(3000*(att+1));continue;}
      ulog('Drop chunk '+cidx+', retry '+(att+1)+'/8');
      await sleep(1500*(att+1));
    }
  }
  throw new Error('Chunk '+cidx+' failed after 8 attempts');
}
async function upOneFile(file,onDone){
  ulog('Getting plan...');
  const plan=await getUploadPlan(file);
  const{tid,chunk_size:cs,total,par,received}=plan;
  _curTid=tid;
  const done=new Set(received||[]);
  const t0=Date.now(); const sentRef={v:done.size};
  ulog(done.size>0?'Resuming: '+done.size+'/'+total+' done':(total+' chunks @'+Math.round(cs/1024/1024)+'MB'));
  document.getElementById('lockwarn').style.display='block';
  const queue=[];
  for(let i=0;i<total;i++) if(!done.has(i)) queue.push(i);
  let qi=0;
  while(qi<queue.length&&!_cancelled){
    await waitR(); if(_cancelled)break;
    const batch=queue.slice(qi,qi+par);
    const results=await Promise.allSettled(batch.map(cidx=>upChunk(file,cidx,plan,t0,sentRef)));
    const failed=[];
    results.forEach((r,i)=>{if(r.status==='fulfilled'&&r.value===true)done.add(batch[i]);else if(!_cancelled)failed.push(batch[i]);});
    if(failed.length>0&&!_cancelled){
      try{
        const np=await getUploadPlan(file);
        const srv=new Set(np.received||[]);
        failed.forEach(c=>{if(srv.has(c)){done.add(c);sentRef.v++;}});
        ulog('Reconciled: '+done.size+'/'+total);
      }catch(e){ulog('Reconcile failed...');}
      const still=failed.filter(c=>!done.has(c));
      if(still.length>0){queue.splice(qi,0,...still);await sleep(2000);continue;}
    }
    qi+=batch.length;
  }
  if(_cancelled)return;
  document.getElementById('lockwarn').style.display='none';
  clearTid(file.name,file.size);
  onDone();
}
async function startUp(){
  if(!selFiles.length)return;
  const btn=document.getElementById('ubtn');
  btn.disabled=true;btn.textContent='Uploading...';
  document.getElementById('pw').style.display='block';
  document.getElementById('ctr').style.display='flex';
  _paused=false;_cancelled=false;_acs=[];ulog('');
  try{
    for(let i=0;i<selFiles.length&&!_cancelled;i++){
      const f=selFiles[i];
      await upOneFile(f,()=>ulog(selFiles.length>1?'File '+(i+1)+'/'+selFiles.length+' done':''));
    }
    ulog(_cancelled?'Cancelled':'All done! \u2713',_cancelled,!_cancelled);
  }catch(e){ulog(_cancelled?'Cancelled':'Error: '+e.message,true);}
  finally{
    document.getElementById('lockwarn').style.display='none';
    btn.textContent='Send More';btn.disabled=false;document.getElementById('ctr').style.display='none';_paused=false;
    btn.onclick=()=>{selFiles=[];document.getElementById('sf').style.display='none';document.getElementById('pw').style.display='none';fi.value='';btn.disabled=true;btn.textContent='Send to PC';btn.onclick=startUp;};
  }
}
function togPause(){
  const pb=document.getElementById('pbtn'),bar=document.getElementById('pbar');
  if(!_paused){_paused=true;_acs.forEach(a=>{try{a.abort();}catch(e){}});_acs=[];
    if(_curTid)fetch('/api/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'pause',transfer_id:_curTid,peer_ip:''})});
    pb.textContent='Resume';pb.style.cssText='background:rgba(0,229,160,.15);color:var(--grn);border:1px solid rgba(0,229,160,.35)';bar.classList.add('paused');ulog('Paused');
  }else{_paused=false;
    if(_curTid)fetch('/api/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'resume',transfer_id:_curTid,peer_ip:''})});
    pb.textContent='Pause';pb.style.cssText='';bar.classList.remove('paused');ulog('');
  }
}
function doCancel(){if(!confirm('Cancel?'))return;_cancelled=true;_paused=false;_acs.forEach(a=>{try{a.abort();}catch(e){}});_acs=[];if(_curTid){fetch('/api/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'cancel',transfer_id:_curTid,peer_ip:''})});selFiles[0]&&clearTid(selFiles[0].name,selFiles[0].size);}}
function loadMedia(){
  const el=document.getElementById('wlist');el.innerHTML='<div style="text-align:center;padding:20px;color:var(--mut)">Loading...</div>';
  fetch('/api/files').then(r=>r.json()).then(files=>{
    const vids=files.filter(f=>f.is_video||f.is_audio);
    if(!vids.length){el.innerHTML='<div style="text-align:center;padding:20px;color:var(--mut);font-size:.82rem">No media yet</div>';return;}
    el.innerHTML='';
    vids.forEach(f=>{const row=document.createElement('div');row.className='fitem';const ico=f.is_video?'&#127916;':'&#127925;';row.innerHTML='<span class="fico">'+ico+'</span><span class="fname" title="'+esc(f.name)+'">'+esc(f.name)+'</span><span class="fsz">'+fmt(f.size)+'</span>'+(f.is_video?'<a class="bw" href="/player/'+f.id+'" target="_blank">Watch</a>':'')+'<a class="bdl" href="/api/download/'+f.id+'" download="'+esc(f.name)+'">&#8595;</a>';el.appendChild(row);});
  }).catch(()=>{el.innerHTML='<div style="color:var(--acc2);padding:14px">Failed</div>';});
}
function loadFiles(){
  const el=document.getElementById('dlist');el.innerHTML='<div style="text-align:center;padding:20px;color:var(--mut)">Loading...</div>';
  fetch('/api/files').then(r=>r.json()).then(files=>{
    if(!files.length){el.innerHTML='<div style="text-align:center;padding:20px;color:var(--mut);font-size:.82rem">No files yet</div>';return;}
    el.innerHTML='';
    files.forEach(f=>{const row=document.createElement('div');row.className='fitem';row.innerHTML='<span class="fico">&#128196;</span><span class="fname" title="'+esc(f.name)+'">'+esc(f.name)+'</span><span class="fsz">'+fmt(f.size)+'</span><a class="bdl" href="/api/download/'+f.id+'" download="'+esc(f.name)+'">&#8595; Save</a>';el.appendChild(row);});
  }).catch(()=>{el.innerHTML='<div style="color:var(--acc2);padding:14px">Failed</div>';});
}
fetch('/api/info').then(r=>r.json()).then(d=>document.getElementById('pcip').textContent=d.ip);
function fmt(b){if(!b)return'0 B';const u=['B','KB','MB','GB','TB'];let i=0;while(b>=1024&&i<4){b/=1024;i++;}return b.toFixed(i?1:0)+' '+u[i];}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
</script></body></html>
"""

# ══════════════════════════════════════════════════════════════
# HTML — VIDEO PLAYER  (FFmpeg transcoding + audio track switching)
# ══════════════════════════════════════════════════════════════
HTML_PLAYER = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SwiftDrop Player</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
html,body{background:#000;height:100%;overflow:hidden;font-family:'DM Sans','Segoe UI',sans-serif;color:#fff}
.pc{position:relative;width:100%;height:100vh;background:#000;cursor:none;user-select:none}.pc.ctl{cursor:default}
video{width:100%;height:100%;object-fit:contain;display:block}
.top-bar{position:absolute;top:0;left:0;right:0;padding:16px 20px;background:linear-gradient(rgba(0,0,0,.8),transparent);display:flex;align-items:center;gap:12px;opacity:0;transition:opacity .3s;pointer-events:none}
.pc.ctl .top-bar{opacity:1;pointer-events:all}
.back-btn{background:rgba(255,255,255,.15);border:none;color:#fff;border-radius:50%;width:34px;height:34px;cursor:pointer;font-size:1rem;display:flex;align-items:center;justify-content:center;flex-shrink:0}
.back-btn:hover{background:rgba(255,255,255,.3)}
.vid-title{font-size:.85rem;opacity:.9;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
.tc-badge{font-size:.65rem;background:rgba(108,99,255,.55);border:1px solid rgba(108,99,255,.8);color:#fff;padding:2px 8px;border-radius:10px;white-space:nowrap;flex-shrink:0;display:none}
.tc-badge.show{display:inline-block}
.ctrl-wrap{position:absolute;bottom:0;left:0;right:0;background:linear-gradient(transparent,rgba(0,0,0,.9));padding:36px 20px 18px;opacity:0;transition:opacity .3s;pointer-events:none}
.pc.ctl .ctrl-wrap{opacity:1;pointer-events:all}
.seek-wrap{position:relative;height:4px;background:rgba(255,255,255,.2);border-radius:2px;cursor:pointer;transition:height .15s;margin-bottom:12px}
.seek-wrap:hover{height:7px}
.seek-buf{position:absolute;height:100%;background:rgba(255,255,255,.3);border-radius:2px;pointer-events:none}
.seek-pos{position:absolute;height:100%;background:#6c63ff;border-radius:2px;pointer-events:none}
.seek-thumb{position:absolute;width:14px;height:14px;background:#fff;border-radius:50%;top:50%;transform:translate(-50%,-50%);display:none;pointer-events:none;box-shadow:0 0 4px rgba(0,0,0,.7)}
.seek-wrap:hover .seek-thumb,.seek-wrap.dragging .seek-thumb{display:block}
.seek-tip{position:absolute;background:rgba(0,0,0,.85);color:#fff;font-size:.68rem;padding:3px 7px;border-radius:4px;bottom:14px;transform:translateX(-50%);white-space:nowrap;pointer-events:none;display:none}
.seek-wrap:hover .seek-tip{display:block}
.cr{display:flex;align-items:center;gap:8px}
.cl{display:flex;align-items:center;gap:8px;flex:1;min-width:0}
.cr-r{display:flex;align-items:center;gap:7px;flex-shrink:0}
.cb{background:none;border:none;color:#fff;cursor:pointer;padding:5px 6px;opacity:.85;font-size:.95rem;line-height:1;transition:opacity .13s;flex-shrink:0;border-radius:5px}
.cb:hover{opacity:1;background:rgba(255,255,255,.1)}.cb.act{color:#6c63ff;opacity:1}
.skip-btn{background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.2);border-radius:6px;color:#fff;cursor:pointer;padding:4px 10px;font-size:.72rem;font-family:monospace;font-weight:700;white-space:nowrap;transition:background .13s}
.skip-btn:hover{background:rgba(255,255,255,.22)}
.vol-g{display:flex;align-items:center;gap:6px}
.vol-sl{-webkit-appearance:none;appearance:none;width:72px;height:3px;background:rgba(255,255,255,.3);border-radius:2px;outline:none;cursor:pointer}
.vol-sl::-webkit-slider-thumb{-webkit-appearance:none;width:13px;height:13px;background:#fff;border-radius:50%}
.vol-sl::-moz-range-thumb{width:13px;height:13px;background:#fff;border-radius:50%;border:none}
.time{font-family:monospace;font-size:.75rem;color:rgba(255,255,255,.8);white-space:nowrap}
.spd-btn{font-family:monospace;font-size:.72rem;font-weight:700;padding:3px 8px;background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.2);border-radius:5px;color:#fff;cursor:pointer}
.spd-btn:hover{background:rgba(255,255,255,.22)}
.audio-wrap{position:relative}
.audio-btn{font-size:.72rem;font-weight:700;padding:4px 10px;background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.2);border-radius:5px;color:#fff;cursor:pointer;white-space:nowrap}
.audio-btn:hover{background:rgba(255,255,255,.22)}
.audio-menu{position:absolute;bottom:calc(100% + 8px);right:0;background:#12121e;border:1px solid rgba(255,255,255,.18);border-radius:9px;min-width:190px;overflow:hidden;display:none;z-index:20;box-shadow:0 8px 24px rgba(0,0,0,.6)}
.audio-menu.open{display:block}
.audio-item{padding:10px 14px;font-size:.78rem;cursor:pointer;transition:background .12s;white-space:nowrap}
.audio-item:hover{background:rgba(108,99,255,.3)}
.audio-item.sel{color:#6c63ff;font-weight:700}
.cflash{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);font-size:3.5rem;opacity:0;pointer-events:none;transition:opacity .12s}.cflash.show{opacity:.85}
.spin{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:44px;height:44px;border:3px solid rgba(255,255,255,.15);border-top-color:#6c63ff;border-radius:50%;animation:spin .7s linear infinite;display:none}.spin.show{display:block}
@keyframes spin{to{transform:translate(-50%,-50%) rotate(360deg)}}
.err{position:absolute;inset:0;display:none;flex-direction:column;align-items:center;justify-content:center;gap:16px;background:rgba(0,0,0,.88);text-align:center;padding:20px}.err.show{display:flex}
.err h2{font-size:1.15rem;font-weight:700}.err p{color:rgba(255,255,255,.6);font-size:.84rem;line-height:1.65;max-width:340px}
.err-btns{display:flex;gap:10px;flex-wrap:wrap;justify-content:center}
.btn-dl{background:#6c63ff;color:#fff;padding:9px 20px;border-radius:8px;font-size:.84rem;font-weight:700;text-decoration:none;border:none;cursor:pointer}
.btn-retry{background:rgba(255,255,255,.12);color:#fff;padding:9px 20px;border-radius:8px;font-size:.84rem;font-weight:700;border:1px solid rgba(255,255,255,.25);cursor:pointer}
.tc-notice{position:absolute;top:62px;right:14px;background:rgba(108,99,255,.88);color:#fff;font-size:.72rem;padding:6px 13px;border-radius:8px;opacity:0;transition:opacity .4s;pointer-events:none}
.tc-notice.show{opacity:1}
.warn-bar{position:absolute;top:62px;left:50%;transform:translateX(-50%);background:rgba(200,50,50,.92);color:#fff;font-size:.72rem;padding:8px 16px;border-radius:8px;text-align:center;max-width:420px;line-height:1.55;display:none;z-index:10}
.warn-bar.show{display:block}
</style></head>
<body>
<div class="pc" id="pc">
  <video id="vid" playsinline preload="metadata"></video>
  <div class="top-bar">
    <button class="back-btn" onclick="history.length>1?history.back():location.href='/'">&#8592;</button>
    <span class="vid-title" id="ttl">Loading...</span>
    <span class="tc-badge" id="tcbadge">&#9889; Transcoding</span>
  </div>
  <div class="tc-notice" id="tcnotice">&#9889; Live transcode via FFmpeg — H.264/AAC</div>
  <div class="warn-bar" id="warnbar"></div>
  <div class="cflash" id="cf"></div>
  <div class="spin" id="sp"></div>
  <div class="err" id="err">
    <h2>Playback error</h2>
    <p id="errmsg">Could not play this file.</p>
    <div class="err-btns">
      <a class="btn-dl" id="errdn" href="#">&#8595; Download</a>
      <button class="btn-retry" onclick="retryPlay()">&#8635; Retry</button>
    </div>
  </div>
  <div class="ctrl-wrap">
    <div class="seek-wrap" id="sw">
      <div class="seek-buf" id="sbuf" style="width:0"></div>
      <div class="seek-pos" id="spos" style="width:0"></div>
      <div class="seek-thumb" id="sthumb" style="left:0"></div>
      <div class="seek-tip" id="stip">0:00</div>
    </div>
    <div class="cr">
      <div class="cl">
        <button class="cb" id="pbtn" onclick="tPlay()" title="Space">&#9654;</button>
        <button class="skip-btn" onclick="skip(-10)" title="←">&#8592; 10s</button>
        <button class="skip-btn" onclick="skip(10)" title="→">10s &#8594;</button>
        <div class="vol-g">
          <button class="cb" id="mbtn" onclick="tMute()" title="M">&#128266;</button>
          <input class="vol-sl" id="vsl" type="range" min="0" max="1" step="0.02" value="1" oninput="setVol(+this.value)">
        </div>
        <span class="time" id="tdisp">0:00 / 0:00</span>
      </div>
      <div class="cr-r">
        <button class="spd-btn" id="spdbtn" onclick="cycleSpd()">1x</button>
        <div class="audio-wrap" id="audwrap" style="display:none">
          <button class="audio-btn" id="audbtn" onclick="toggleAudioMenu()">&#127925; Audio</button>
          <div class="audio-menu" id="audmenu"></div>
        </div>
        <button class="cb" id="ccbtn" onclick="tCC()" title="C">CC</button>
        <button class="cb" onclick="tFS()" title="F">&#9974;</button>
      </div>
    </div>
  </div>
</div>
<script>
const fid=location.pathname.split('/').pop();
const vid=document.getElementById('vid'), pc=document.getElementById('pc');
let _curAudio=0, _transcoded=false, _probe={};

// Probe codec + audio tracks
fetch('/api/probe/'+fid).then(r=>r.json()).then(info=>{
  _probe=info; _transcoded=info.needs_transcode||false;
  if(_transcoded){
    document.getElementById('tcbadge').classList.add('show');
    const n=document.getElementById('tcnotice');
    n.classList.add('show'); setTimeout(()=>n.classList.remove('show'),4500);
  }
  if(info.ffmpeg_available===false&&info.needs_transcode){
    const wb=document.getElementById('warnbar');
    wb.textContent='⚠ This file needs FFmpeg to play (HEVC/MKV/AVI). Place ffmpeg.exe next to SwiftDrop and restart.';
    wb.classList.add('show');
  }
  const streams=info.audio_streams||[];
  if(streams.length>1){
    const wrap=document.getElementById('audwrap'), menu=document.getElementById('audmenu');
    wrap.style.display='';
    streams.forEach((s,i)=>{
      const item=document.createElement('div'); item.className='audio-item'+(i===0?' sel':'');
      item.dataset.idx=String(i);
      item.textContent='\uD83D\uDD0A '+(s.label||('Track '+(i+1)))+' ('+s.codec+')';
      item.addEventListener('click',()=>switchAudio(i)); menu.appendChild(item);
    });
  }
}).catch(()=>{});

// Title + download link
fetch('/api/files').then(r=>r.json()).then(files=>{
  const f=files.find(x=>x.id===fid);
  if(f){document.title=f.name+' - SwiftDrop';document.getElementById('ttl').textContent=f.name;document.getElementById('errdn').href='/api/download/'+fid;}
}).catch(()=>{});

// Subtitle track
const tr=document.createElement('track');tr.kind='subtitles';tr.label='Sub';tr.srclang='en';tr.src='/subtitles/'+fid;tr.default=false;vid.appendChild(tr);

function buildSrc(ai){return '/stream/'+fid+(ai>0?'?audio='+ai:'');}
vid.src=buildSrc(0);

function switchAudio(idx){
  _curAudio=idx;
  document.querySelectorAll('.audio-item').forEach(el=>el.classList.toggle('sel',parseInt(el.dataset.idx)===idx));
  document.getElementById('audmenu').classList.remove('open');
  const t=vid.currentTime; vid.src=buildSrc(idx); vid.load();
  vid.addEventListener('loadeddata',()=>{vid.currentTime=t;vid.play();},{once:true});
  const s=(_probe.audio_streams||[])[idx];
  if(s)document.getElementById('audbtn').textContent='\uD83D\uDD0A '+(s.label||'Track '+(idx+1));
}
function toggleAudioMenu(){document.getElementById('audmenu').classList.toggle('open');}
document.addEventListener('click',e=>{if(!e.target.closest('.audio-wrap'))document.getElementById('audmenu').classList.remove('open');});

// Controls show/hide
let hideT=null,ctlOn=false;
function showCtl(){pc.classList.add('ctl');ctlOn=true;clearTimeout(hideT);if(!vid.paused)hideT=setTimeout(()=>{pc.classList.remove('ctl');ctlOn=false;},3500);}
pc.addEventListener('mousemove',showCtl);
pc.addEventListener('touchstart',()=>{ctlOn?pc.classList.remove('ctl'):showCtl();ctlOn=!ctlOn;},{passive:true});

// Play/pause
function tPlay(){vid.paused?vid.play():vid.pause();}
pc.addEventListener('click',e=>{if(e.target===pc||e.target===vid){tPlay();showCtl();}});
vid.addEventListener('play',()=>{document.getElementById('pbtn').innerHTML='&#9646;&#9646;';showCtl();});
vid.addEventListener('pause',()=>{document.getElementById('pbtn').innerHTML='&#9654;';showCtl();});
function flash(t){const e=document.getElementById('cf');e.innerHTML=t;e.classList.add('show');setTimeout(()=>e.classList.remove('show'),500);}
vid.addEventListener('play',()=>flash('&#9654;'));
vid.addEventListener('pause',()=>flash('&#9646;&#9646;'));

// Skip ±10s
function skip(sec){
  vid.currentTime=Math.max(0,Math.min(vid.duration||0,vid.currentTime+sec));
  flash(sec>0?'+10s':'-10s'); showCtl();
}

// Seek bar
const sw=document.getElementById('sw'); let seeking=false;
vid.addEventListener('timeupdate',()=>{
  if(!vid.duration||seeking)return;
  const p=vid.currentTime/vid.duration*100;
  document.getElementById('spos').style.width=p+'%';
  document.getElementById('sthumb').style.left=p+'%';
  document.getElementById('tdisp').textContent=ft(vid.currentTime)+' / '+ft(vid.duration);
});
vid.addEventListener('progress',()=>{
  if(!vid.duration||!vid.buffered.length)return;
  try{document.getElementById('sbuf').style.width=(vid.buffered.end(vid.buffered.length-1)/vid.duration*100)+'%';}catch(e){}
});
sw.addEventListener('mousemove',e=>{
  const r=sw.getBoundingClientRect(),p=Math.max(0,Math.min(1,(e.clientX-r.left)/r.width));
  document.getElementById('stip').textContent=ft(p*(vid.duration||0));
  document.getElementById('stip').style.left=(p*100)+'%';
});
function doSeek(e){
  if(!vid.duration)return;
  const r=sw.getBoundingClientRect(),p=Math.max(0,Math.min(1,(e.clientX-r.left)/r.width));
  vid.currentTime=p*vid.duration;
}
sw.addEventListener('click',doSeek);
sw.addEventListener('mousedown',e=>{seeking=true;sw.classList.add('dragging');});
document.addEventListener('mouseup',e=>{if(seeking){seeking=false;sw.classList.remove('dragging');doSeek(e);}});
document.addEventListener('mousemove',e=>{
  if(!seeking)return;
  const r=sw.getBoundingClientRect(),p=Math.max(0,Math.min(1,(e.clientX-r.left)/r.width));
  document.getElementById('spos').style.width=(p*100)+'%';
  document.getElementById('sthumb').style.left=(p*100)+'%';
  document.getElementById('tdisp').textContent=ft(p*(vid.duration||0))+' / '+ft(vid.duration||0);
});

// Volume
function setVol(v){vid.volume=v;vid.muted=v===0;updMute();}
function tMute(){vid.muted=!vid.muted;document.getElementById('vsl').value=vid.muted?0:vid.volume;updMute();}
function updMute(){document.getElementById('mbtn').innerHTML=vid.muted||vid.volume===0?'&#128264;':'&#128266;';}

// Speed
const SPDS=[0.5,0.75,1,1.25,1.5,2];let si=2;
function cycleSpd(){si=(si+1)%SPDS.length;vid.playbackRate=SPDS[si];document.getElementById('spdbtn').textContent=SPDS[si]+'x';}

// Subtitles
let ccOn=false;
function tCC(){ccOn=!ccOn;for(let i=0;i<vid.textTracks.length;i++)vid.textTracks[i].mode=ccOn?'showing':'hidden';document.getElementById('ccbtn').classList.toggle('act',ccOn);}

// Fullscreen
function tFS(){if(!document.fullscreenElement)pc.requestFullscreen().catch(()=>{});else document.exitFullscreen();}

// Buffering spinner
vid.addEventListener('waiting',()=>document.getElementById('sp').classList.add('show'));
vid.addEventListener('playing',()=>document.getElementById('sp').classList.remove('show'));
vid.addEventListener('canplay',()=>document.getElementById('sp').classList.remove('show'));

// Error
function retryPlay(){document.getElementById('err').classList.remove('show');vid.src=buildSrc(_curAudio);vid.load();vid.play().catch(()=>{});}
vid.addEventListener('error',()=>{
  const e=document.getElementById('err');e.classList.add('show');
  const code=vid.error?vid.error.code:0;
  const msgs={1:'Load aborted',2:'Network error — check Wi-Fi',3:'Decode error — codec not supported',4:'Format not supported — put ffmpeg.exe next to app'};
  document.getElementById('errmsg').textContent=msgs[code]||'Unknown error';
});

// Keyboard shortcuts
document.addEventListener('keydown',e=>{
  if(e.target.tagName==='INPUT')return; showCtl();
  switch(e.key){
    case ' ':e.preventDefault();tPlay();break;
    case 'ArrowLeft':e.preventDefault();skip(-10);break;
    case 'ArrowRight':e.preventDefault();skip(10);break;
    case 'ArrowUp':e.preventDefault();vid.volume=Math.min(1,vid.volume+.1);document.getElementById('vsl').value=vid.volume;break;
    case 'ArrowDown':e.preventDefault();vid.volume=Math.max(0,vid.volume-.1);document.getElementById('vsl').value=vid.volume;break;
    case 'm':case 'M':tMute();break;
    case 'f':case 'F':tFS();break;
    case 'c':case 'C':tCC();break;
  }
});
function ft(s){const m=Math.floor(s/60),ss=Math.floor(s%60);return m+':'+String(ss).padStart(2,'0');}
</script></body></html>
"""

# ══════════════════════════════════════════════════════════════
# HTTP SERVER
# ══════════════════════════════════════════════════════════════
class _QuickHTTPServer(ThreadingHTTPServer):
    def get_request(self):
        conn,addr=super().get_request()
        try: conn.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
        except Exception: pass
        return conn,addr

class UIHandler(BaseHTTPRequestHandler):
    def log_message(self,*_): pass

    def do_DELETE(self):
        path=self.path.split("?")[0]
        if path.startswith("/api/delete/"):
            fid=path[len("/api/delete/"):]; ok=_delete_file(fid)
            self._ok("application/json",json.dumps({"ok":ok}).encode())
        else: self._respond(404,"text/plain",b"Not found")

    def do_GET(self):
        ua=self.headers.get("User-Agent",""); path=self.path.split("?")[0]
        if path in ("/","/index.html"):
            if _is_mobile(ua): self.send_response(302); self.send_header("Location","/mobile"); self.end_headers()
            else: self._ok("text/html; charset=utf-8",HTML_DESKTOP.encode())
        elif path=="/mobile":             self._ok("text/html; charset=utf-8",HTML_MOBILE.encode())
        elif path.startswith("/player/"): self._ok("text/html; charset=utf-8",HTML_PLAYER.encode())
        elif path=="/api/info":           self._ok("application/json",json.dumps({"ip":get_local_ip(),"version":"6"}).encode())
        elif path=="/api/progress":       self._ok("application/json",json.dumps(TM.all_progress()).encode())
        elif path=="/api/files":          self._ok("application/json",json.dumps(_files_list()).encode())
        elif path=="/api/devices":        self._ok("application/json",json.dumps(get_devices()).encode())
        elif path.startswith("/api/upload-plan"):
            handle_upload_plan(self)
        elif path.startswith("/api/upload-status/"):
            parsed=urlparse(self.path); qs=parse_qs(parsed.query)
            tid=parsed.path[len("/api/upload-status/"):]
            req_fname=unquote(qs.get("fname",[""])[0]); req_fsize=int(qs.get("fsize",["0"])[0])
            rec=TM.get(tid)
            if rec: chunks=rec.received_list()
            else:
                chunks=[]
                if req_fname and TMP_DIR.exists():
                    tmp=_tmp_path(req_fname); sc=_sc_load(tmp)
                    if sc.get("fsize")==req_fsize: chunks=sc.get("chunks",[])
            self._ok("application/json",json.dumps({"received_chunks":chunks}).encode())
        elif path.startswith("/api/probe/"):
            # Return codec + audio track info for the player
            fid=path[len("/api/probe/"):]; fpath=_get_file(fid)
            if not fpath:
                self._ok("application/json",json.dumps({"error":"not_found"}).encode()); return
            info=_ffprobe(fpath)
            info["needs_transcode"]=bool(FFMPEG and _needs_transcode(fpath))
            info["ffmpeg_available"]=FFMPEG is not None
            self._ok("application/json",json.dumps(info).encode())
        elif path.startswith("/api/download/"):
            fid=path[len("/api/download/"):]; fpath=_get_file(fid)
            if not fpath: self._respond(404,"text/plain",b"Not found"); return
            serve_ranged(self,fpath,inline=False)
        elif path.startswith("/stream/"):
            parsed=urlparse(self.path); qs=parse_qs(parsed.query)
            fid=parsed.path[len("/stream/"):]
            try: ai=int(qs.get("audio",["0"])[0])
            except Exception: ai=0
            fpath=_get_file(fid)
            if not fpath: self._respond(404,"text/plain",b"Not found"); return
            serve_ranged(self,fpath,inline=True,audio_index=ai)
        elif path.startswith("/subtitles/"):
            fid=path[len("/subtitles/"):]; fpath=_get_file(fid)
            if not fpath: self._respond(204,"text/vtt",b""); return
            srt=fpath.with_suffix(".srt")
            if not srt.exists(): self._respond(204,"text/vtt",b""); return
            try:
                vtt=srt_to_vtt(srt.read_text(encoding="utf-8",errors="replace")).encode()
                self.send_response(200); self.send_header("Content-Type","text/vtt; charset=utf-8")
                self.send_header("Content-Length",str(len(vtt))); self.send_header("Access-Control-Allow-Origin","*"); self.end_headers(); self.wfile.write(vtt)
            except Exception: self._respond(204,"text/vtt",b"")
        else: self._respond(404,"text/plain",b"Not found")

    def do_POST(self):
        path=self.path.split("?")[0]
        if path=="/api/send":
            length=int(self.headers.get("Content-Length",0)); body=self.rfile.read(length)
            try:
                peer_ip,fpath=self._parse_mp(self.headers.get("Content-Type",""),body); tid=str(uuid.uuid4())
                threading.Thread(target=_run_send_job,args=({"path":str(fpath),"peer_ip":peer_ip,"tid":tid,"is_tmp":True},),daemon=True).start()
                self._ok("application/json",json.dumps({"ok":True,"transfer_id":tid}).encode())
            except Exception as e:
                log.error(f"Send: {e}",exc_info=True); self._respond(500,"application/json",json.dumps({"error":str(e)}).encode())
        elif path=="/api/mobile-chunk":    handle_mobile_chunk(self)
        elif path=="/api/mobile-finalize": handle_mobile_finalize(self)
        elif path=="/api/control":
            try:
                body=json.loads(self.rfile.read(int(self.headers.get("Content-Length",0))).decode())
                action=body.get("action",""); tid=body.get("transfer_id",""); peer=body.get("peer_ip","").strip()
                if   action=="pause":  ok=TM.pause(tid);  (peer and pause_send(tid,peer))
                elif action=="resume": ok=TM.resume(tid); (peer and resume_send(tid,peer))
                elif action=="cancel": ok=TM.cancel(tid); (peer and cancel_send(tid,peer))
                else: ok=False
                self._ok("application/json",json.dumps({"ok":ok}).encode())
            except Exception as e: self._respond(400,"application/json",json.dumps({"error":str(e)}).encode())
        elif path=="/api/exit":
            self._ok("application/json",json.dumps({"ok":True}).encode())
            def _bye(): time.sleep(0.5); _shutdown.set(); os._exit(0)
            threading.Thread(target=_bye,daemon=True).start()
        else: self._respond(404,"text/plain",b"Not found")

    def _ok(self,ct,body): self._respond(200,ct,body)

    def _respond(self,code,ct,body):
        try:
            self.send_response(code); self.send_header("Content-Type",ct)
            self.send_header("Content-Length",str(len(body))); self.send_header("Access-Control-Allow-Origin","*")
            self.end_headers(); self.wfile.write(body)
        except (ConnectionResetError,BrokenPipeError,OSError): pass

    def _parse_mp(self,ctype,body):
        bnd=None
        for p in ctype.split(";"):
            p=p.strip()
            if p.startswith("boundary="): bnd=p[9:].strip('"').encode()
        if not bnd: raise ValueError("no boundary")
        fields={}; fdata=None; fname=None
        for part in body.split(b"--"+bnd)[1:]:
            if part in (b"--\r\n",b"--"): continue
            if b"\r\n\r\n" not in part: continue
            hr,content=part.split(b"\r\n\r\n",1); content=content.rstrip(b"\r\n")
            hdrs=hr.decode(errors="replace")
            disp=next((l for l in hdrs.splitlines() if l.lower().startswith("content-disposition")),"")
            name=fn=""
            for seg in disp.split(";"):
                seg=seg.strip()
                if seg.startswith('name="'): name=seg[6:-1]
                elif seg.startswith('filename="'): fn=seg[10:-1]
            if fn: fdata=content; fname=Path(fn).name
            else: fields[name]=content.decode(errors="replace")
        peer_ip=fields.get("peer_ip","").strip()
        if not peer_ip: raise ValueError("peer_ip missing")
        if not fdata or not fname: raise ValueError("no file")
        TMP_DIR.mkdir(parents=True,exist_ok=True); tmp=TMP_DIR/fname
        with open(tmp,"wb") as fh: fh.write(fdata)
        return peer_ip,tmp

def run_ui_server():
    srv=_QuickHTTPServer(("0.0.0.0",UI_PORT),UIHandler)
    log.info(f"UI: http://localhost:{UI_PORT}  Mobile: http://{get_local_ip()}:{UI_PORT}/mobile")
    t=threading.Thread(target=srv.serve_forever,daemon=True); t.start()
    _shutdown.wait(); srv.shutdown()

# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════
def main():
    global FFMPEG
    OUTPUT_DIR.mkdir(parents=True,exist_ok=True)
    TMP_DIR.mkdir(parents=True,exist_ok=True)
    _cleanup_stale_tmp()
    _registry_refresh()

    # FIX: resolve FFMPEG here, after BASE_DIR is set
    FFMPEG = _find_ffmpeg()

    ip=get_local_ip()
    print("="*64)
    print("  SwiftDrop v6 — Integrity-First LAN Transfer")
    print("="*64)
    print(f"  IP       : {ip}")
    print(f"  PC UI    : http://localhost:{UI_PORT}")
    print(f"  Mobile   : http://{ip}:{UI_PORT}/mobile")
    print(f"  Files    : {OUTPUT_DIR}")
    print(f"  FFmpeg   : {FFMPEG or 'NOT FOUND (transcoding disabled)'}")
    print("="*64)
    print(f"  TCP      : {CHUNK_SIZE//1024//1024}MB chunks x {THREADS} workers")
    print(f"  Mobile   : server-planned chunks (2-8 MB), CRC32 per chunk")
    print(f"  Resume   : automatic (sidecar + sessionStorage)")
    if not FFMPEG:
        print()
        print("  NOTE: Place ffmpeg.exe next to this file to enable")
        print("        transcoding of HEVC/MKV/AVI files in the player.")
    print()
    threading.Thread(target=run_tcp_server,daemon=True).start()
    threading.Thread(target=run_ui_server, daemon=True).start()
    threading.Thread(target=run_discovery, daemon=True).start()
    time.sleep(0.8)
    webbrowser.open(f"http://localhost:{UI_PORT}")
    try:
        while not _shutdown.is_set(): time.sleep(0.5)
    except KeyboardInterrupt: pass
    print("SwiftDrop stopped.")

if __name__ == "__main__":
    main()