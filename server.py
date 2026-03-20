"""
server.py — High-Performance Chunked File Transfer Server
Runs on the LAPTOP (receiver).

Architecture:
  - Listens on a configurable TCP port.
  - Each incoming connection is handled by a dedicated thread from a thread pool.
  - Chunks are written directly to their correct byte offset in the final output
    file using seek+write, eliminating a separate reassembly step.
  - SHA-256 integrity is verified per chunk before acknowledging.
  - A lightweight length-prefixed JSON control protocol drives the handshake.
  - Once the client signals FINALIZE, the server verifies the whole-file hash.
  - Resume-safe: interrupted transfers can be continued without re-sending
    already-confirmed chunks.

Usage:
    python server.py [--host 0.0.0.0] [--port 9000] [--output-dir ./received]
                     [--workers 16] [--log-level INFO]
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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


# ─── Constants ────────────────────────────────────────────────────────────────

HEADER_SIZE = 4           # 4-byte big-endian uint32 — JSON envelope length
BUFFER_SIZE = 1 << 17    # 128 KiB socket recv buffer
VERSION     = "1.0"


# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(threadName)-14s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("server")


# ─── Protocol helpers ─────────────────────────────────────────────────────────

def recv_exact(sock: socket.socket, n: int) -> bytes:
    """Read exactly *n* bytes; raise EOFError on a premature connection close."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(min(n - len(buf), BUFFER_SIZE))
        if not chunk:
            raise EOFError(f"Connection closed after {len(buf)}/{n} bytes")
        buf.extend(chunk)
    return bytes(buf)


def send_msg(sock: socket.socket, payload: dict) -> None:
    """Send a length-prefixed JSON message."""
    data = json.dumps(payload).encode()
    sock.sendall(struct.pack(">I", len(data)) + data)


def recv_msg(sock: socket.socket) -> dict:
    """Receive a length-prefixed JSON message."""
    raw_len = recv_exact(sock, HEADER_SIZE)
    length  = struct.unpack(">I", raw_len)[0]
    return json.loads(recv_exact(sock, length))


# ─── Transfer state (shared across worker threads for a single file) ──────────

class TransferState:
    """
    Thread-safe bookkeeper for one in-progress file transfer.

    The output file is pre-allocated at INIT time so that concurrent workers
    can each seek to their own offset and write independently — no locking
    needed on different file regions (OS-level guarantee for pwrite semantics
    is simulated here via seek+write protected by a per-transfer file lock).
    """

    def __init__(self, file_path: Path, total_chunks: int,
                 file_size: int, file_hash: str):
        self.file_path      = file_path
        self.total_chunks   = total_chunks
        self.file_size      = file_size
        self.expected_hash  = file_hash

        self._lock          = threading.Lock()
        self._file_lock     = threading.Lock()   # serialise seek+write pairs
        self._received      : set[int] = set()   # confirmed written chunk ids
        self._bytes_written = 0
        self._start_time    = time.monotonic()

        # Pre-allocate so random-offset writes are safe
        with open(file_path, "wb") as f:
            f.truncate(file_size)
        log.info("Pre-allocated %s (%.2f GiB)", file_path.name,
                 file_size / (1 << 30))

    # ── Chunk write ───────────────────────────────────────────────────────────

    def write_chunk(self, offset: int, data: bytes) -> None:
        """Write *data* at *offset* in the output file (thread-safe)."""
        with self._file_lock:
            with open(self.file_path, "r+b") as f:
                f.seek(offset)
                f.write(data)

    def record_chunk(self, index: int, size: int) -> bool:
        """Mark chunk as received; return True when all chunks are done."""
        with self._lock:
            self._received.add(index)
            self._bytes_written += size
            all_done = len(self._received) == self.total_chunks
        self._log_progress()
        return all_done

    def is_chunk_received(self, index: int) -> bool:
        with self._lock:
            return index in self._received

    def received_indices(self) -> list[int]:
        with self._lock:
            return list(self._received)

    # ── Progress ──────────────────────────────────────────────────────────────

    def _log_progress(self) -> None:
        with self._lock:
            received = len(self._received)
            written  = self._bytes_written
        elapsed = time.monotonic() - self._start_time
        pct     = received / self.total_chunks * 100
        speed   = written / elapsed / (1 << 20) if elapsed > 0 else 0.0
        log.info("Progress %d/%d chunks (%.1f%%) — %.1f MiB/s",
                 received, self.total_chunks, pct, speed)

    # ── Final verification ────────────────────────────────────────────────────

    def verify_file(self) -> bool:
        log.info("Verifying whole-file SHA-256 of %s …", self.file_path.name)
        sha = hashlib.sha256()
        with open(self.file_path, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                sha.update(block)
        actual = sha.hexdigest()
        ok = actual == self.expected_hash
        if ok:
            log.info("✓ Integrity OK: %s", actual)
        else:
            log.error("✗ Hash mismatch  expected=%s  actual=%s",
                      self.expected_hash, actual)
        return ok

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._start_time


# ─── Per-connection handler ───────────────────────────────────────────────────

class ConnectionHandler:
    """
    Handles exactly one accepted TCP connection.

    A connection carries one of three message types:
      INIT      — client registers a new (or resuming) transfer
      CHUNK     — client ships one data chunk
      FINALIZE  — client requests end-to-end integrity check
    """

    def __init__(self, conn: socket.socket, addr,
                 state_registry: dict, registry_lock: threading.Lock,
                 output_dir: Path):
        self.conn            = conn
        self.addr            = addr
        self.state_registry  = state_registry
        self.registry_lock   = registry_lock
        self.output_dir      = output_dir

    def run(self) -> None:
        try:
            msg = recv_msg(self.conn)
            cmd = msg.get("cmd")
            dispatch = {
                "INIT":     self._handle_init,
                "CHUNK":    self._handle_chunk,
                "FINALIZE": self._handle_finalize,
            }
            handler = dispatch.get(cmd)
            if handler:
                handler(msg)
            else:
                self._reply({"status": "error",
                             "reason": f"Unknown command: {cmd!r}"})
        except (EOFError, ConnectionResetError, BrokenPipeError) as exc:
            log.warning("Connection %s lost: %s", self.addr, exc)
        except Exception:
            log.exception("Unhandled error for connection %s", self.addr)
        finally:
            self.conn.close()

    # ── INIT ──────────────────────────────────────────────────────────────────

    def _handle_init(self, msg: dict) -> None:
        transfer_id  = msg["transfer_id"]
        filename     = Path(msg["filename"]).name   # prevent path traversal
        file_size    = int(msg["file_size"])
        total_chunks = int(msg["total_chunks"])
        file_hash    = msg["file_hash"]
        dest         = self.output_dir / filename

        with self.registry_lock:
            if transfer_id in self.state_registry:
                state   = self.state_registry[transfer_id]
                already = state.received_indices()
                log.info("Resume transfer %s — %d/%d chunks already received",
                         transfer_id, len(already), total_chunks)
                self._reply({"status": "ok", "resume": True,
                             "received_chunks": already})
                return

            state = TransferState(dest, total_chunks, file_size, file_hash)
            self.state_registry[transfer_id] = state

        log.info("INIT transfer %s  file=%s  size=%.2f GiB  chunks=%d",
                 transfer_id, filename, file_size / (1 << 30), total_chunks)
        self._reply({"status": "ok", "resume": False, "received_chunks": []})

    # ── CHUNK ─────────────────────────────────────────────────────────────────

    def _handle_chunk(self, msg: dict) -> None:
        transfer_id = msg["transfer_id"]
        index       = int(msg["index"])
        offset      = int(msg["offset"])
        length      = int(msg["length"])
        chunk_hash  = msg["hash"]

        with self.registry_lock:
            state = self.state_registry.get(transfer_id)
        if state is None:
            self._reply({"status": "error", "reason": "Unknown transfer_id"})
            return

        # Idempotent: drain socket then ACK without re-writing
        if state.is_chunk_received(index):
            recv_exact(self.conn, length)
            self._reply({"status": "ok", "index": index, "action": "skipped"})
            return

        data = recv_exact(self.conn, length)

        # Per-chunk integrity check
        actual_hash = hashlib.sha256(data).hexdigest()
        if actual_hash != chunk_hash:
            log.warning("Chunk %d hash mismatch from %s — requesting retry",
                        index, self.addr)
            self._reply({"status": "retry", "index": index,
                         "reason": "hash_mismatch"})
            return

        state.write_chunk(offset, data)
        all_done = state.record_chunk(index, length)
        self._reply({"status": "ok", "index": index, "action": "written"})

        if all_done:
            log.info("All %d chunks received for transfer %s",
                     state.total_chunks, transfer_id)

    # ── FINALIZE ──────────────────────────────────────────────────────────────

    def _handle_finalize(self, msg: dict) -> None:
        transfer_id = msg["transfer_id"]

        with self.registry_lock:
            state = self.state_registry.get(transfer_id)
        if state is None:
            self._reply({"status": "error", "reason": "Unknown transfer_id"})
            return

        missing = [i for i in range(state.total_chunks)
                   if not state.is_chunk_received(i)]
        if missing:
            log.warning("Finalize requested but %d chunks missing", len(missing))
            self._reply({"status": "incomplete", "missing_chunks": missing})
            return

        ok      = state.verify_file()
        elapsed = state.elapsed
        speed   = state.file_size / elapsed / (1 << 20) if elapsed > 0 else 0.0

        if ok:
            with self.registry_lock:
                del self.state_registry[transfer_id]
            log.info("✓ Transfer %s complete in %.1fs  avg=%.1f MiB/s",
                     transfer_id, elapsed, speed)
            self._reply({"status": "ok", "verified": True,
                         "elapsed_s": round(elapsed, 2),
                         "avg_mib_s": round(speed, 2)})
        else:
            self._reply({"status": "error", "verified": False,
                         "reason": "whole_file_hash_mismatch"})

    def _reply(self, payload: dict) -> None:
        send_msg(self.conn, payload)


# ─── Server ───────────────────────────────────────────────────────────────────

class FileTransferServer:

    def __init__(self, host: str, port: int, output_dir: Path, workers: int):
        self.host       = host
        self.port       = port
        self.output_dir = output_dir
        self.workers    = workers
        output_dir.mkdir(parents=True, exist_ok=True)

        self._state_registry: dict[str, TransferState] = {}
        self._registry_lock = threading.Lock()
        self._executor      = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="worker"
        )

    def serve_forever(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
            srv.bind((self.host, self.port))
            srv.listen(self.workers * 4)
            log.info("═" * 60)
            log.info("  File Transfer Server  v%s", VERSION)
            log.info("  Listening  : %s:%d", self.host, self.port)
            log.info("  Output dir : %s", self.output_dir.resolve())
            log.info("  Workers    : %d", self.workers)
            log.info("═" * 60)
            try:
                while True:
                    conn, addr = srv.accept()
                    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    conn.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
                    handler = ConnectionHandler(
                        conn, addr,
                        self._state_registry, self._registry_lock,
                        self.output_dir,
                    )
                    self._executor.submit(handler.run)
            except KeyboardInterrupt:
                log.info("Interrupted — shutting down cleanly.")
            finally:
                self._executor.shutdown(wait=False)


# ─── Entry point ──────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="High-performance chunked file transfer server",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--host",       default="0.0.0.0",
                   help="Address to bind (0.0.0.0 = all interfaces)")
    p.add_argument("--port",       type=int, default=9000, help="TCP port")
    p.add_argument("--output-dir", default="./received",
                   help="Directory where received files are stored")
    p.add_argument("--workers",    type=int, default=16,
                   help="Thread-pool size (should match client --threads)")
    p.add_argument("--log-level",  default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p


if __name__ == "__main__":
    args = build_parser().parse_args()
    logging.getLogger().setLevel(args.log_level)
    FileTransferServer(
        host       = args.host,
        port       = args.port,
        output_dir = Path(args.output_dir),
        workers    = args.workers,
    ).serve_forever()
