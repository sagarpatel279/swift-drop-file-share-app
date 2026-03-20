"""
client.py — High-Performance Chunked File Transfer Client
Runs on the DESKTOP PC (sender).

Architecture:
  - Computes the whole-file SHA-256 hash up front for end-to-end verification.
  - Splits the file into fixed-size chunks (default 64 MiB).
  - Sends an INIT message to the server and receives the list of already-done
    chunks (enables transparent resume after interruption).
  - Uploads remaining chunks concurrently via a configurable thread pool,
    each thread opening its own persistent TCP connection.
  - Per-chunk SHA-256 is sent alongside the data; the server verifies it.
  - Failed chunks are retried up to MAX_RETRIES times with exponential backoff.
  - Sends a FINALIZE message and displays confirmed server-side statistics.
  - Rich progress bar via tqdm (falls back gracefully if not installed).

Usage:
    python client.py <file> <server_host> [--port 9000] [--chunk-size 67108864]
                     [--threads 8] [--max-retries 5] [--log-level INFO]
"""

import argparse
import hashlib
import json
import logging
import os
import socket
import struct
import threading
import time
import uuid
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from tqdm import tqdm as _tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False

# ─── Constants ────────────────────────────────────────────────────────────────

HEADER_SIZE   = 4            # 4-byte big-endian uint32 — JSON envelope length
SOCKET_BUF    = 1 << 20     # 1 MiB socket send/recv buffer
DEFAULT_CHUNK = 64 << 20    # 64 MiB default chunk size
MAX_RETRIES   = 5
BASE_BACKOFF  = 1.0          # seconds; doubles on each retry
VERSION       = "1.0"

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(threadName)-14s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("client")


# ─── Protocol helpers ─────────────────────────────────────────────────────────

def recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(min(n - len(buf), SOCKET_BUF))
        if not chunk:
            raise EOFError(f"Connection closed after {len(buf)}/{n} bytes")
        buf.extend(chunk)
    return bytes(buf)


def send_msg(sock: socket.socket, payload: dict) -> None:
    data = json.dumps(payload).encode()
    sock.sendall(struct.pack(">I", len(data)) + data)


def recv_msg(sock: socket.socket) -> dict:
    raw_len = recv_exact(sock, HEADER_SIZE)
    length  = struct.unpack(">I", raw_len)[0]
    return json.loads(recv_exact(sock, length))


def make_socket(host: str, port: int) -> socket.socket:
    """Create a tuned TCP socket connected to *host*:*port*."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.setsockopt(socket.SOL_SOCKET,  socket.SO_SNDBUF,   SOCKET_BUF)
    sock.setsockopt(socket.SOL_SOCKET,  socket.SO_RCVBUF,   SOCKET_BUF)
    sock.connect((host, port))
    return sock


# ─── Hash helpers ─────────────────────────────────────────────────────────────

def sha256_file(path: Path) -> str:
    """Return hex SHA-256 of the entire file (streaming, low RAM)."""
    log.info("Computing whole-file SHA-256 for %s …", path.name)
    sha = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            sha.update(block)
    digest = sha.hexdigest()
    log.info("File hash: %s", digest)
    return digest


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ─── Chunk descriptor ─────────────────────────────────────────────────────────

class Chunk:
    __slots__ = ("index", "offset", "length")

    def __init__(self, index: int, offset: int, length: int):
        self.index  = index
        self.offset = offset
        self.length = length


def compute_chunks(file_size: int, chunk_size: int) -> list[Chunk]:
    chunks = []
    offset = 0
    idx    = 0
    while offset < file_size:
        length = min(chunk_size, file_size - offset)
        chunks.append(Chunk(idx, offset, length))
        offset += length
        idx    += 1
    return chunks


# ─── Progress tracker ─────────────────────────────────────────────────────────

class Progress:
    """Thread-safe progress reporter; uses tqdm when available."""

    def __init__(self, total_bytes: int, total_chunks: int):
        self._lock         = threading.Lock()
        self._bytes_sent   = 0
        self._chunks_done  = 0
        self._total_bytes  = total_bytes
        self._total_chunks = total_chunks
        self._start        = time.monotonic()
        self._bar          = None

        if HAS_TQDM:
            self._bar = _tqdm(
                total=total_bytes,
                unit="B",
                unit_scale=True,
                unit_divisor=1024,
                desc="Uploading",
                dynamic_ncols=True,
            )

    def update(self, nbytes: int) -> None:
        with self._lock:
            self._bytes_sent  += nbytes
            self._chunks_done += 1
            if self._bar:
                self._bar.update(nbytes)
            else:
                elapsed = time.monotonic() - self._start
                speed   = self._bytes_sent / elapsed / (1 << 20) if elapsed else 0
                pct     = self._bytes_sent / self._total_bytes * 100
                log.info("Sent %d/%d chunks  %.1f%%  %.1f MiB/s",
                         self._chunks_done, self._total_chunks, pct, speed)

    def close(self) -> None:
        if self._bar:
            self._bar.close()

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._start


# ─── Single-chunk uploader ────────────────────────────────────────────────────

def upload_chunk(chunk: Chunk, file_path: Path,
                 host: str, port: int, transfer_id: str,
                 progress: Progress, retries: int = MAX_RETRIES) -> int:
    """
    Read *chunk* from *file_path* and upload it to the server.
    Returns the chunk index on success; raises after exhausting retries.
    """
    # Read the slice once; reuse across retry attempts
    with open(file_path, "rb") as f:
        f.seek(chunk.offset)
        data = f.read(chunk.length)

    data_hash = sha256_bytes(data)

    for attempt in range(1, retries + 1):
        try:
            with make_socket(host, port) as sock:
                send_msg(sock, {
                    "cmd":         "CHUNK",
                    "transfer_id": transfer_id,
                    "index":       chunk.index,
                    "offset":      chunk.offset,
                    "length":      chunk.length,
                    "hash":        data_hash,
                })
                sock.sendall(data)
                reply = recv_msg(sock)

            status = reply.get("status")
            if status in ("ok", "skipped"):
                if reply.get("action") != "skipped":
                    progress.update(chunk.length)
                return chunk.index

            if status == "retry":
                log.warning("Server requested retry for chunk %d (attempt %d/%d)",
                            chunk.index, attempt, retries)
            else:
                log.warning("Unexpected status %r for chunk %d (attempt %d/%d)",
                            status, chunk.index, attempt, retries)

        except (OSError, EOFError) as exc:
            log.warning("Network error on chunk %d attempt %d: %s",
                        chunk.index, attempt, exc)

        if attempt < retries:
            backoff = BASE_BACKOFF * (2 ** (attempt - 1))
            log.info("Retrying chunk %d in %.1fs …", chunk.index, backoff)
            time.sleep(backoff)

    raise RuntimeError(
        f"Chunk {chunk.index} failed after {retries} attempts"
    )


# ─── Main client logic ────────────────────────────────────────────────────────

class FileTransferClient:

    def __init__(self, file_path: Path, host: str, port: int,
                 chunk_size: int, threads: int, max_retries: int):
        self.file_path   = file_path
        self.host        = host
        self.port        = port
        self.chunk_size  = chunk_size
        self.threads     = threads
        self.max_retries = max_retries

    # ── Public entry point ────────────────────────────────────────────────────

    def send(self) -> None:
        file_size  = self.file_path.stat().st_size
        file_hash  = sha256_file(self.file_path)
        chunks     = compute_chunks(file_size, self.chunk_size)
        total_chunks = len(chunks)
        transfer_id  = str(uuid.uuid4())

        log.info("═" * 60)
        log.info("  File       : %s", self.file_path.name)
        log.info("  Size       : %.2f GiB", file_size / (1 << 30))
        log.info("  Chunks     : %d × %.0f MiB",
                 total_chunks, self.chunk_size / (1 << 20))
        log.info("  Threads    : %d", self.threads)
        log.info("  Server     : %s:%d", self.host, self.port)
        log.info("  Transfer ID: %s", transfer_id)
        log.info("═" * 60)

        # ── INIT handshake ────────────────────────────────────────────────────
        already_received = self._init(transfer_id, file_size,
                                      total_chunks, file_hash)
        already_set = set(already_received)
        pending = [c for c in chunks if c.index not in already_set]

        if already_received:
            log.info("Resuming: %d/%d chunks already on server",
                     len(already_received), total_chunks)

        progress = Progress(
            total_bytes  = sum(c.length for c in pending),
            total_chunks = len(pending),
        )

        # ── Parallel upload ───────────────────────────────────────────────────
        t0 = time.monotonic()
        self._upload_parallel(pending, transfer_id, progress)
        progress.close()

        elapsed  = time.monotonic() - t0
        data_mib = sum(c.length for c in pending) / (1 << 20)
        log.info("Upload finished in %.1fs  (%.1f MiB/s)",
                 elapsed, data_mib / elapsed if elapsed > 0 else 0)

        # ── FINALIZE ──────────────────────────────────────────────────────────
        self._finalize(transfer_id)

    # ── INIT ──────────────────────────────────────────────────────────────────

    def _init(self, transfer_id: str, file_size: int,
              total_chunks: int, file_hash: str) -> list[int]:
        with make_socket(self.host, self.port) as sock:
            send_msg(sock, {
                "cmd":          "INIT",
                "transfer_id":  transfer_id,
                "filename":     self.file_path.name,
                "file_size":    file_size,
                "total_chunks": total_chunks,
                "file_hash":    file_hash,
            })
            reply = recv_msg(sock)

        if reply.get("status") != "ok":
            raise RuntimeError(f"INIT rejected: {reply}")

        return reply.get("received_chunks", [])

    # ── Parallel upload ───────────────────────────────────────────────────────

    def _upload_parallel(self, pending: list[Chunk],
                         transfer_id: str, progress: Progress) -> None:
        if not pending:
            log.info("Nothing to upload — all chunks already on server.")
            return

        failed = []
        with ThreadPoolExecutor(max_workers=self.threads,
                                thread_name_prefix="uploader") as pool:
            futures = {
                pool.submit(
                    upload_chunk,
                    chunk, self.file_path,
                    self.host, self.port,
                    transfer_id, progress,
                    self.max_retries,
                ): chunk
                for chunk in pending
            }
            for future in as_completed(futures):
                chunk = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    log.error("Chunk %d permanently failed: %s",
                              chunk.index, exc)
                    failed.append(chunk.index)

        if failed:
            raise RuntimeError(
                f"Transfer failed: {len(failed)} chunk(s) could not be sent: "
                f"{sorted(failed)}"
            )

    # ── FINALIZE ──────────────────────────────────────────────────────────────

    def _finalize(self, transfer_id: str) -> None:
        log.info("Sending FINALIZE and waiting for server verification …")
        with make_socket(self.host, self.port) as sock:
            send_msg(sock, {"cmd": "FINALIZE", "transfer_id": transfer_id})
            reply = recv_msg(sock)

        status = reply.get("status")
        if status == "ok" and reply.get("verified"):
            log.info("✓ Server confirmed file integrity!")
            log.info("  Server elapsed : %.1fs", reply.get("elapsed_s", 0))
            log.info("  Server avg rate: %.1f MiB/s", reply.get("avg_mib_s", 0))
        elif status == "incomplete":
            missing = reply.get("missing_chunks", [])
            raise RuntimeError(
                f"Server reports {len(missing)} missing chunks: {missing[:20]}"
                f"{'…' if len(missing) > 20 else ''}"
            )
        else:
            raise RuntimeError(f"Finalize failed: {reply}")


# ─── Entry point ──────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="High-performance chunked file transfer client",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("file",        help="Path to the file to transfer")
    p.add_argument("host",        help="Server IP address (laptop's hotspot IP)")
    p.add_argument("--port",      type=int, default=9000,      help="Server port")
    p.add_argument("--chunk-size",type=int, default=DEFAULT_CHUNK,
                   help="Chunk size in bytes (default 64 MiB)")
    p.add_argument("--threads",   type=int, default=8,
                   help="Number of concurrent upload threads")
    p.add_argument("--max-retries",type=int, default=MAX_RETRIES,
                   help="Max retry attempts per chunk")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p


if __name__ == "__main__":
    args = build_parser().parse_args()
    logging.getLogger().setLevel(args.log_level)

    file_path = Path(args.file)
    if not file_path.is_file():
        raise SystemExit(f"Error: file not found: {file_path}")

    client = FileTransferClient(
        file_path   = file_path,
        host        = args.host,
        port        = args.port,
        chunk_size  = args.chunk_size,
        threads     = args.threads,
        max_retries = args.max_retries,
    )

    try:
        client.send()
    except RuntimeError as exc:
        raise SystemExit(f"\n✗ Transfer failed: {exc}") from exc
